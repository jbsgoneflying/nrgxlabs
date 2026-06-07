"""Phase 1 module 5 — path generator (regime-conditional bootstrap MVP).

Replaces v1's plain bootstrap Monte Carlo for breach probability with a
regime-conditional resampler: given a corpus of (date → realized
log-return) pairs and an optional regime weighting, draw N synthetic
forward paths and report the implied breach probability + quantile
bands.

This is the MVP rung on the Phase 2 conditional-diffusion ladder.
The API surface (``PathSampler.sample_paths`` / ``breach_probability``)
is stable so swapping in a learned generator later doesn't touch the
router or dashboard.

Pure Python — uses the standard ``random`` module for reproducibility.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence


# ── Sampler ────────────────────────────────────────────────


@dataclass
class PathStats:
    """Summary statistics for a batch of sampled paths."""

    n_samples: int
    horizon_days: int
    terminal_mean: float
    terminal_std: float
    terminal_quantiles: dict[str, float]  # "p05", "p25", "p50", "p75", "p95"

    def to_dict(self) -> dict:
        return {
            "n_samples": self.n_samples,
            "horizon_days": self.horizon_days,
            "terminal_mean": self.terminal_mean,
            "terminal_std": self.terminal_std,
            "terminal_quantiles": self.terminal_quantiles,
        }


@dataclass
class BreachResult:
    """Output of a breach-probability resample."""

    p_breach: float
    p_breach_interval: tuple[float, float]
    p_lower_breach: float
    p_upper_breach: float
    n_samples: int
    horizon_days: int
    lower_threshold: float | None
    upper_threshold: float | None
    path_stats: PathStats

    def to_dict(self) -> dict:
        return {
            "p_breach": self.p_breach,
            "p_breach_interval": list(self.p_breach_interval),
            "p_lower_breach": self.p_lower_breach,
            "p_upper_breach": self.p_upper_breach,
            "n_samples": self.n_samples,
            "horizon_days": self.horizon_days,
            "lower_threshold": self.lower_threshold,
            "upper_threshold": self.upper_threshold,
            "path_stats": self.path_stats.to_dict(),
        }


@dataclass
class PathSampler:
    """Pure-Python bootstrap path generator with optional regime weights.

    Parameters
    ----------
    returns:
        Sequence of historical 1-period log-returns (e.g. daily log-returns
        of an underlying). At least ``MIN_RETURNS`` are required.
    weights:
        Optional non-negative weights aligned with ``returns``. The sampler
        draws each step proportionally to these weights — wire this from
        the regime index's nearest-day similarities to get
        regime-conditional paths. ``None`` means uniform sampling
        (vanilla bootstrap).
    rng:
        Optional ``random.Random`` instance for deterministic tests.
    """

    MIN_RETURNS = 20  # at least 20 historical samples to be meaningful

    returns: list[float] = field(default_factory=list)
    weights: list[float] | None = None
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        clean: list[float] = []
        clean_weights: list[float] | None = [] if self.weights is not None else None
        for i, r in enumerate(self.returns):
            try:
                f = float(r)
            except (TypeError, ValueError):
                continue
            if math.isnan(f) or math.isinf(f):
                continue
            clean.append(f)
            if clean_weights is not None:
                w = self.weights[i] if i < len(self.weights) else 0.0
                try:
                    fw = float(w)
                except (TypeError, ValueError):
                    fw = 0.0
                clean_weights.append(max(0.0, fw))
        self.returns = clean
        if clean_weights is not None:
            if not any(w > 0 for w in clean_weights):
                clean_weights = None  # collapse to uniform
            self.weights = clean_weights

    @property
    def n_returns(self) -> int:
        return len(self.returns)

    def is_warm(self) -> bool:
        return self.n_returns >= self.MIN_RETURNS

    def sample_paths(
        self,
        *,
        n_samples: int,
        horizon_days: int,
    ) -> list[list[float]]:
        """Return ``n_samples`` cumulative-log-return paths of length
        ``horizon_days``. Each step is a bootstrap draw from ``returns``."""
        if not self.is_warm():
            raise RuntimeError(
                f"PathSampler not warm: have {self.n_returns} returns, "
                f"need >= {self.MIN_RETURNS}"
            )
        if n_samples <= 0 or horizon_days <= 0:
            return []

        if self.weights is None:
            draw = self._uniform_draw
        else:
            draw = self._weighted_draw_factory()

        paths: list[list[float]] = []
        for _ in range(int(n_samples)):
            cum = 0.0
            path = []
            for _ in range(int(horizon_days)):
                cum += draw()
                path.append(cum)
            paths.append(path)
        return paths

    def breach_probability(
        self,
        *,
        lower_threshold: float | None,
        upper_threshold: float | None,
        n_samples: int = 5000,
        horizon_days: int = 21,
        bootstrap_ci_resamples: int = 200,
    ) -> BreachResult:
        """Compute the probability that the cumulative log-return path
        exits the (lower_threshold, upper_threshold) bracket at any point
        before horizon_days. Either threshold can be ``None`` to disable
        that side (so the call works for one-sided breaches).

        Returns the point estimate plus a 95% bootstrap CI.
        """
        if lower_threshold is None and upper_threshold is None:
            raise ValueError("at least one threshold must be provided")

        paths = self.sample_paths(n_samples=n_samples, horizon_days=horizon_days)
        breach_flags: list[bool] = []
        lower_flags: list[bool] = []
        upper_flags: list[bool] = []
        terminals: list[float] = []
        for path in paths:
            terminals.append(path[-1])
            lo_breach = up_breach = False
            if lower_threshold is not None:
                lo_breach = any(step <= lower_threshold for step in path)
            if upper_threshold is not None:
                up_breach = any(step >= upper_threshold for step in path)
            breach_flags.append(lo_breach or up_breach)
            lower_flags.append(lo_breach)
            upper_flags.append(up_breach)

        n = len(breach_flags) or 1
        p_breach = sum(1 for f in breach_flags if f) / n
        p_lower = sum(1 for f in lower_flags if f) / n
        p_upper = sum(1 for f in upper_flags if f) / n

        # Bootstrap CI by resampling the breach-flag vector.
        ci = _bootstrap_proportion_ci(
            breach_flags, n_resamples=bootstrap_ci_resamples, alpha=0.05, rng=self.rng,
        )

        stats = self._terminal_stats(terminals, horizon_days)
        return BreachResult(
            p_breach=round(p_breach, 4),
            p_breach_interval=(round(ci[0], 4), round(ci[1], 4)),
            p_lower_breach=round(p_lower, 4),
            p_upper_breach=round(p_upper, 4),
            n_samples=int(n_samples),
            horizon_days=int(horizon_days),
            lower_threshold=lower_threshold,
            upper_threshold=upper_threshold,
            path_stats=stats,
        )

    # ── Internal draw helpers ─────────────────────────────

    def _uniform_draw(self) -> float:
        return self.rng.choice(self.returns)

    def _weighted_draw_factory(self):
        # Pre-compute the cumulative weight vector once.
        cum: list[float] = []
        running = 0.0
        for w in self.weights or []:
            running += float(w)
            cum.append(running)
        total = cum[-1] if cum else 0.0

        def draw() -> float:
            r = self.rng.random() * total
            # Linear scan is fine for our corpus sizes (<10k); a binary
            # search would only matter past 100k draws.
            for i, c in enumerate(cum):
                if r <= c:
                    return self.returns[i]
            return self.returns[-1]
        return draw

    def _terminal_stats(self, terminals: Sequence[float], horizon_days: int) -> PathStats:
        if not terminals:
            return PathStats(
                n_samples=0,
                horizon_days=horizon_days,
                terminal_mean=0.0,
                terminal_std=0.0,
                terminal_quantiles={k: 0.0 for k in ("p05", "p25", "p50", "p75", "p95")},
            )
        mean = statistics.fmean(terminals)
        sd = statistics.pstdev(terminals)
        return PathStats(
            n_samples=len(terminals),
            horizon_days=horizon_days,
            terminal_mean=round(mean, 6),
            terminal_std=round(sd, 6),
            terminal_quantiles={
                "p05": round(_quantile(terminals, 0.05), 6),
                "p25": round(_quantile(terminals, 0.25), 6),
                "p50": round(_quantile(terminals, 0.50), 6),
                "p75": round(_quantile(terminals, 0.75), 6),
                "p95": round(_quantile(terminals, 0.95), 6),
            },
        )


# ── Helpers ────────────────────────────────────────────────


def _quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile. ``values`` does not need to be sorted."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def _bootstrap_proportion_ci(
    flags: Sequence[bool],
    *,
    n_resamples: int = 200,
    alpha: float = 0.05,
    rng: random.Random,
) -> tuple[float, float]:
    """Percentile bootstrap CI for a proportion."""
    n = len(flags)
    if n == 0:
        return (0.0, 0.0)
    proportions: list[float] = []
    for _ in range(int(n_resamples)):
        s = sum(1 for _ in range(n) if rng.choice(flags))
        proportions.append(s / n)
    return (
        max(0.0, _quantile(proportions, alpha / 2)),
        min(1.0, _quantile(proportions, 1 - alpha / 2)),
    )


def regime_weights_from_neighbors(
    neighbors: Iterable[Mapping[str, float]],
    *,
    return_dates: Sequence[str],
) -> list[float]:
    """Convert regime-encoder ``search`` neighbors into a weight vector
    aligned with ``return_dates``.

    Each neighbor is expected to expose ``date`` and ``similarity``.
    Days that don't appear in the neighbor list get weight 0; days that
    do get their similarity. This couples the path generator to the
    regime encoder without any cross-module imports — the caller passes
    the neighbor list explicitly.
    """
    sim_by_date: dict[str, float] = {}
    for n in neighbors:
        if not isinstance(n, Mapping):
            continue
        date = n.get("date")
        sim = n.get("similarity")
        if date is None or sim is None:
            continue
        try:
            sim_by_date[str(date)] = max(0.0, float(sim))
        except (TypeError, ValueError):
            continue
    weights: list[float] = []
    for d in return_dates:
        weights.append(sim_by_date.get(str(d), 0.0))
    return weights
