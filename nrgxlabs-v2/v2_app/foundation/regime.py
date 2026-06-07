"""Phase 1 module 3 — regime encoder (feature-space MVP).

The Foundation Brain's "where are we" layer. Given a daily market state,
returns:

  - a z-scored embedding vector
  - the top-K most similar historical days (cross-time, single-market)
  - the regime cluster the day belongs to (using v1's existing label as
    the ground-truth class for now; Phase 2 swaps the encoder for a
    learned latent space and clusters over IT)

This MVP is the same proven recipe as module 2 (analogues): pure-Python
hand-crafted feature extraction over v1's existing front_layer:dms:*
schema, z-scored, then masked cosine search. No numpy, no torch, no
training corpus, no GPU — and immediately useful.

Phase 2 swaps ``extract_market_state`` for a small Transformer / Mamba
encoder over the rolling 60-day multi-asset panel, keeping the index
API stable so the swap is internal.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .vectorspace import (
    Standardizer,
    feature_coverage,
    masked_cosine,
)

LOG = logging.getLogger("v2.regime")


# ── Feature space ─────────────────────────────────────────


# Each entry: (name, [path...], optional categorical_map).
# Paths are searched in order on the DMS document; first non-null wins.
# Categorical maps convert e.g. "backwardation" → -1.0.
_FEATURES: list[tuple[str, list[list[str]], dict[str, float] | None]] = [
    # ── Headline regime ──
    ("regimeScore",         [["regime", "score"]],                   None),
    # ── Vol structure ──
    ("volLevel",            [["vol_state", "level"]],                None),
    ("volTermStructure",    [["vol_state", "term_structure"]],
        {"contango": 1.0, "flat": 0.0, "backwardation": -1.0}),
    ("volSkew",             [["vol_state", "skew"]],
        {"low": -1.0, "neutral": 0.0, "elevated": 1.0}),
    # ── News risk ──
    ("newsRiskToday",       [["news_risk", "today"]],
        {"low": 0.0, "medium": 1.0, "high": 2.0}),
    # ── Engine gates as composite "openness" score ──
    ("engineGatesOpen",     [["engine_gates"]],                      None),  # special handler
    # ── Earnings landscape ──
    ("earningsCandidatesN", [["earnings_candidates"]],               None),  # special handler
    ("earningsTopScore",    [["earnings_candidates"]],               None),  # special handler
]

FEATURE_NAMES: list[str] = [name for name, _, _ in _FEATURES]


_GATE_OPEN_SCORE = {
    "allowed":    1.0,
    "watch":      0.66,
    "selective":  0.5,
    "reduced":    0.33,
    "suppressed": 0.0,
}


# Order matters for clusters: the regime label is the v1 ground-truth class.
REGIME_LABELS = ["Risk-On", "Transitional", "Risk-Off", "Stressed"]


def regime_label(dms: Mapping[str, Any]) -> str | None:
    """Return v1's regime label (Risk-On / Transitional / Risk-Off / Stressed)."""
    label = _safe_get(dms, "regime", "state")
    if isinstance(label, str) and label in REGIME_LABELS:
        return label
    # Older snapshots used ``label`` instead of ``state``.
    label2 = _safe_get(dms, "regime", "label")
    if isinstance(label2, str) and label2 in REGIME_LABELS:
        return label2
    return None


def extract_market_state(dms: Mapping[str, Any]) -> dict[str, float | None]:
    """Pull every regime feature for a daily market state document.

    Missing or non-numeric paths return ``None``, which the index treats as
    a masked dimension at search time (same semantics as the analogue index).

    Skeleton-default docs (v1's ``build_daily_market_state`` fallback when
    no engines fed it real data) return all-None so the index builder skips
    them. v1 stamps these with ``data_quality.skeleton_default = True`` and
    we also detect them by fingerprint for backward compat with historical
    docs written before the v1 hygiene hotfix.
    """
    if is_skeleton_default(dms):
        return {name: None for name, _, _ in _FEATURES}
    out: dict[str, float | None] = {}
    for name, paths, cat_map in _FEATURES:
        out[name] = _resolve(dms, name, paths, cat_map)
    return out


def is_skeleton_default(dms: Mapping[str, Any]) -> bool:
    """Detect DMS docs that are pure build_daily_market_state defaults.

    Two-tier check:
      1. Explicit tag from the v1 hygiene hotfix
         (``data_quality.skeleton_default == True``).
      2. Fingerprint match for historical docs written before the hotfix:
         regime.state == "Transitional" AND regime.score == 50 AND
         vol_state.level == 25 AND term_structure == "flat" AND
         skew == "neutral" AND news_risk.today == "low" AND no
         meaningful regime.probs / regime.confidence.
    """
    dq = dms.get("data_quality") if isinstance(dms, Mapping) else None
    if isinstance(dq, Mapping) and bool(dq.get("skeleton_default")):
        return True

    regime = dms.get("regime") or {}
    vol = dms.get("vol_state") or {}
    news = dms.get("news_risk") or {}
    fingerprint = (
        _safe_get(regime, "state") == "Transitional"
        and _safe_get(regime, "score") == 50.0
        and _safe_get(vol, "level") == 25.0
        and _safe_get(vol, "term_structure") == "flat"
        and _safe_get(vol, "skew") == "neutral"
        and _safe_get(news, "today") == "low"
    )
    if not fingerprint:
        return False
    # The fingerprint is permissive — any genuinely neutral day matches it.
    # If the doc carries an mi_snap-derived signal (probs with non-default
    # mass, or confidence > 0.5), trust it as real and don't filter.
    probs = regime.get("probs") if isinstance(regime, Mapping) else None
    if isinstance(probs, Mapping) and probs:
        try:
            risk_on = float(probs.get("risk_on", 0.0) or 0.0)
            stressed = float(probs.get("stressed", 0.0) or 0.0)
            if risk_on > 0.25 or stressed > 0.25:
                return False
        except (TypeError, ValueError):
            pass
    confidence = regime.get("confidence") if isinstance(regime, Mapping) else None
    try:
        if float(confidence or 0.0) > 0.5:
            return False
    except (TypeError, ValueError):
        pass
    return True


def _resolve(
    dms: Mapping[str, Any],
    name: str,
    paths: list[list[str]],
    cat_map: dict[str, float] | None,
) -> float | None:
    # Special-case derived features — these aren't simple path lookups.
    if name == "engineGatesOpen":
        return _engine_gates_score(_safe_get(dms, "engine_gates"))
    if name == "earningsCandidatesN":
        cands = _safe_get(dms, "earnings_candidates")
        if isinstance(cands, list):
            return float(len(cands))
        return None
    if name == "earningsTopScore":
        cands = _safe_get(dms, "earnings_candidates")
        if isinstance(cands, list) and cands:
            scores = []
            for c in cands:
                if isinstance(c, dict):
                    s = c.get("score")
                    try:
                        scores.append(float(s))
                    except (TypeError, ValueError):
                        continue
            if scores:
                return max(scores)
        return None

    for path in paths:
        v = _safe_get(dms, *path)
        if v is None:
            continue
        if cat_map is not None:
            if isinstance(v, str):
                key = v.strip().lower()
                if key in cat_map:
                    return float(cat_map[key])
            continue
        try:
            f = float(v)
            if math.isnan(f) or math.isinf(f):
                continue
            return f
        except (TypeError, ValueError):
            continue
    return None


def _engine_gates_score(gates: Any) -> float | None:
    """Composite "engines open for business" score in [0, 1].

    Maps each gate's status string to a continuous openness value and
    averages. A higher score means more engines are clear to trade.
    """
    if not isinstance(gates, dict) or not gates:
        return None
    vals: list[float] = []
    for v in gates.values():
        if not isinstance(v, str):
            continue
        s = _GATE_OPEN_SCORE.get(v.strip().lower())
        if s is not None:
            vals.append(s)
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _safe_get(doc: Any, *path: str) -> Any:
    cur: Any = doc
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


# ── Index ──────────────────────────────────────────────────


@dataclass
class IndexedDay:
    date: str
    label: str | None
    z_vec: list[float]
    mask: list[int]
    features: dict[str, float | None]
    summary: dict[str, Any]


@dataclass
class RegimeIndex:
    standardizer: Standardizer
    feature_names: list[str]
    rows: list[IndexedDay] = field(default_factory=list)

    @property
    def n_indexed(self) -> int:
        return len(self.rows)

    def label_distribution(self) -> dict[str, int]:
        out: dict[str, int] = {label: 0 for label in REGIME_LABELS}
        for r in self.rows:
            if r.label and r.label in out:
                out[r.label] += 1
        return out

    def encode(self, market_state: Mapping[str, float | None]) -> dict[str, Any]:
        """Z-score a market state and return its embedding + label distribution
        of the K=10 nearest historical days (a cheap proxy for cluster
        probabilities until the learned encoder ships)."""
        z, mask, names = self.standardizer.transform(market_state)
        nbrs = self.search(market_state, k=10)
        cluster_prior = self._cluster_prior(nbrs)
        return {
            "embedding": z,
            "mask": mask,
            "feature_names": names,
            "n_indexed": self.n_indexed,
            "knn_label_distribution": cluster_prior,
            "knn_top": [
                {"date": n["date"], "label": n["label"], "similarity": n["similarity"]}
                for n in nbrs[:5]
            ],
        }

    def search(
        self,
        market_state: Mapping[str, float | None],
        *,
        k: int = 10,
        date_exclude: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self.rows:
            return []
        q_z, q_mask, _ = self.standardizer.transform(market_state)
        scored: list[tuple[float, IndexedDay]] = []
        for row in self.rows:
            if date_exclude and row.date == date_exclude:
                continue
            sim = masked_cosine(q_z, q_mask, row.z_vec, row.mask)
            scored.append((sim, row))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [
            {
                "date": r.date,
                "label": r.label,
                "similarity": round(float(sim), 4),
                "features": r.features,
                "summary": r.summary,
            }
            for sim, r in scored[: max(1, int(k))]
        ]

    @staticmethod
    def _cluster_prior(neighbors: list[Mapping[str, Any]]) -> dict[str, float]:
        if not neighbors:
            return {label: 0.0 for label in REGIME_LABELS}
        counts: dict[str, int] = {label: 0 for label in REGIME_LABELS}
        for n in neighbors:
            label = n.get("label")
            if label in counts:
                counts[label] += 1
        total = sum(counts.values()) or 1
        return {k: round(v / total, 3) for k, v in counts.items()}

    # ── Persistence ──

    def to_json(self) -> str:
        return json.dumps(
            {
                "feature_names": self.feature_names,
                "standardizer": self.standardizer.to_json(),
                "rows": [
                    {
                        "date": r.date,
                        "label": r.label,
                        "z_vec": r.z_vec,
                        "mask": r.mask,
                        "features": r.features,
                        "summary": r.summary,
                    }
                    for r in self.rows
                ],
            }
        )

    @classmethod
    def from_json(cls, blob: str | None) -> "RegimeIndex | None":
        if not blob:
            return None
        try:
            d = json.loads(blob)
        except Exception:
            return None
        rows = [
            IndexedDay(
                date=str(r.get("date", "")),
                label=r.get("label"),
                z_vec=[float(x) for x in (r.get("z_vec") or [])],
                mask=[int(x) for x in (r.get("mask") or [])],
                features={k: (float(v) if isinstance(v, (int, float)) else None)
                          for k, v in (r.get("features") or {}).items()},
                summary=dict(r.get("summary") or {}),
            )
            for r in (d.get("rows") or [])
        ]
        return cls(
            standardizer=Standardizer.from_json(d.get("standardizer")),
            feature_names=list(d.get("feature_names") or []),
            rows=rows,
        )


# ── Build pipeline ─────────────────────────────────────────


def build_index_from_dms_history(
    dms_docs: Iterable[Mapping[str, Any]],
) -> tuple[RegimeIndex, dict[str, Any]]:
    """Build a regime index from an iterable of DMS documents (one per day).

    Returns (index, stats) with per-feature coverage and label distribution
    so the operator can audit the corpus quality.
    """
    docs = list(dms_docs)
    feature_rows: list[dict[str, float | None]] = []
    metadata_rows: list[dict[str, Any]] = []
    skipped = {
        "no_date": 0,
        "all_features_missing": 0,
        "skeleton_default": 0,
    }

    for d in docs:
        date = str(d.get("date") or "").strip()
        if not date:
            skipped["no_date"] += 1
            continue
        if is_skeleton_default(d):
            skipped["skeleton_default"] += 1
            continue
        feats = extract_market_state(d)
        if not any(v is not None for v in feats.values()):
            skipped["all_features_missing"] += 1
            continue
        feature_rows.append(feats)
        metadata_rows.append({
            "date": date,
            "label": regime_label(d),
            "summary": _summarize(d),
        })

    standardizer = Standardizer()
    standardizer.fit(feature_rows)

    rows: list[IndexedDay] = []
    for feats, meta in zip(feature_rows, metadata_rows):
        z, mask, _ = standardizer.transform(feats)
        rows.append(IndexedDay(
            date=meta["date"],
            label=meta["label"],
            z_vec=z,
            mask=mask,
            features=feats,
            summary=meta["summary"],
        ))

    coverage = feature_coverage(feature_rows, FEATURE_NAMES)
    index = RegimeIndex(
        standardizer=standardizer,
        feature_names=FEATURE_NAMES,
        rows=rows,
    )
    stats = {
        "ok": True,
        "n_seen": len(docs),
        "n_indexed": len(rows),
        "skipped": skipped,
        "feature_names": FEATURE_NAMES,
        "feature_coverage": coverage,
        "label_distribution": index.label_distribution(),
    }
    return index, stats


def _summarize(d: Mapping[str, Any]) -> dict[str, Any]:
    """Tight summary of a DMS doc for nearest-day responses (no full payload)."""
    return {
        "regime_score": _safe_get(d, "regime", "score"),
        "regime_state": _safe_get(d, "regime", "state"),
        "vol_level":    _safe_get(d, "vol_state", "level"),
        "term_structure": _safe_get(d, "vol_state", "term_structure"),
        "skew":         _safe_get(d, "vol_state", "skew"),
        "news_risk_today": _safe_get(d, "news_risk", "today"),
        "earnings_n":   len(_safe_get(d, "earnings_candidates") or []),
    }
