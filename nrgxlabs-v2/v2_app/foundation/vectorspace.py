"""Shared vector-space infrastructure used by every v2 retrieval module.

Both the analogue index (Phase 1 module 2) and the regime encoder
(Phase 1 module 3) need the same primitives:

  - z-score Standardizer with masked-NaN handling at fit + transform
  - masked cosine similarity between two z-scored vectors
  - JSON round-trip for both

Lifting them here keeps a single source of truth, so a fix in the math
flows to every consumer at once.

Pure Python. No numpy. With <100K vectors and <50 features brute-force
cosine sweep is sub-millisecond and the dependency surface stays tiny.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


# ── Standardizer ───────────────────────────────────────────


@dataclass
class Standardizer:
    """Per-feature z-score with masked-NaN handling.

    ``means`` and ``stds`` map feature name → statistic computed at fit time.
    Missing values during fit are skipped (don't affect mean/std). Missing
    during transform → 0 in the z-scored vector AND a 0 in the mask so
    the cosine kernel can ignore that column on both sides.
    """

    means: dict[str, float] = field(default_factory=dict)
    stds: dict[str, float] = field(default_factory=dict)

    def fit(self, rows: Iterable[Mapping[str, float | None]]) -> None:
        rows = list(rows)
        if not rows:
            return
        names = list(rows[0].keys())
        for name in names:
            xs = [r.get(name) for r in rows if r.get(name) is not None]
            if not xs:
                self.means[name] = 0.0
                self.stds[name] = 1.0  # avoid div-by-zero downstream
                continue
            m = sum(xs) / len(xs)
            var = sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1)
            sd = math.sqrt(var) if var > 0 else 1.0
            self.means[name] = float(m)
            self.stds[name] = float(sd)

    def transform(
        self, row: Mapping[str, float | None]
    ) -> tuple[list[float], list[int], list[str]]:
        names = list(self.means.keys()) or list(row.keys())
        z: list[float] = []
        mask: list[int] = []
        for name in names:
            v = row.get(name)
            if v is None:
                z.append(0.0)
                mask.append(0)
                continue
            sd = self.stds.get(name, 1.0) or 1.0
            z.append((float(v) - self.means.get(name, 0.0)) / sd)
            mask.append(1)
        return z, mask, names

    def to_json(self) -> dict[str, Any]:
        return {"means": self.means, "stds": self.stds}

    @classmethod
    def from_json(cls, blob: Mapping[str, Any] | None) -> "Standardizer":
        if not blob:
            return cls()
        return cls(
            means={k: float(v) for k, v in (blob.get("means") or {}).items()},
            stds={k: float(v) for k, v in (blob.get("stds") or {}).items()},
        )


# ── Masked cosine similarity ───────────────────────────────


def masked_cosine(
    a: list[float], a_mask: list[int], b: list[float], b_mask: list[int]
) -> float:
    """Cosine similarity over the mask intersection of two z-scored vectors.

    Returns 0.0 when there are no shared observed dimensions or when either
    side has zero norm on the intersection.
    """
    if not a or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    n = 0
    for ai, am, bi, bm in zip(a, a_mask, b, b_mask):
        if not (am and bm):
            continue
        dot += ai * bi
        na += ai * ai
        nb += bi * bi
        n += 1
    if n == 0 or na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ── Coverage helper ────────────────────────────────────────


def feature_coverage(
    rows: list[Mapping[str, float | None]], names: list[str]
) -> dict[str, float]:
    """Fraction of rows where each feature is present (non-null)."""
    if not rows:
        return {n: 0.0 for n in names}
    n = len(rows)
    return {
        name: round(sum(1 for r in rows if r.get(name) is not None) / n, 3)
        for name in names
    }
