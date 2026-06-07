"""Split-conformal prediction — Foundation Brain Layer 1, module 1.

This is the trust layer. It wraps any point-prediction the v1 (or v2) engines
emit and turns it into a coverage interval with a guaranteed marginal coverage
of ``1 - alpha`` under the exchangeability assumption.

Why split-conformal?

- Distribution-free. No assumption about the residual distribution shape.
- Marginal coverage is guaranteed: P(y ∈ interval) ≥ 1 - α as ``n → ∞``,
  with finite-sample correction ``(1 + 1/n)`` already baked into the quantile.
- Works on top of any black-box predictor — including v1's hand-tuned VRP
  and gamma scores — without retraining or modifying the predictor.
- Can be made adaptive by maintaining a rolling window of nonconformity
  scores so the calibration tracks regime changes.

References:
    Vovk, Gammerman, Shafer (2005). "Algorithmic Learning in a Random World."
    Lei et al. (2018). "Distribution-Free Predictive Inference for Regression."

This module is intentionally pure (no I/O, no Redis, no global state).
``conformal_store`` wraps it with a Redis-backed window for production use.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class ConformalInterval:
    """One calibrated interval around a point prediction."""

    point: float
    lower: float
    upper: float
    alpha: float
    n_calibration: int
    warmup: bool
    bound_lo: float | None = None
    bound_hi: float | None = None

    @property
    def width(self) -> float:
        return float(self.upper - self.lower)

    @property
    def coverage_target(self) -> float:
        return float(1.0 - self.alpha)


@dataclass
class CalibrationState:
    """Rolling buffer of nonconformity scores for one (engine, metric)."""

    scores: list[float] = field(default_factory=list)
    buf_size: int = 1000
    bound_lo: float | None = None
    bound_hi: float | None = None
    last_observation_ts: str | None = None

    def add(self, score: float) -> None:
        self.scores.append(float(score))
        # Keep newest ``buf_size`` only — adapts to regime change without
        # losing exchangeability semantics under "swapping" assumptions.
        if len(self.scores) > self.buf_size:
            del self.scores[0 : len(self.scores) - self.buf_size]

    @property
    def n(self) -> int:
        return len(self.scores)


# ── Pure helpers ───────────────────────────────────────────────────────


def nonconformity(prediction: float, realized: float) -> float:
    """Absolute residual — the simplest, most-used nonconformity score.

    For probabilities in [0, 1] this lives in [0, 1]. For unbounded
    continuous outputs this is just |y - ŷ|.
    """
    return abs(float(realized) - float(prediction))


def quantile_with_finite_sample_correction(
    scores: Iterable[float], alpha: float
) -> float:
    """Conformal quantile of the calibration scores.

    The classic split-conformal formula uses the ``ceil((n + 1) * (1 - α)) / n``
    quantile of the calibration scores (smallest score ≥ that quantile),
    which is equivalent to inflating the standard ``1 - α`` quantile by the
    finite-sample correction ``(1 + 1/n)``. This guarantees marginal
    coverage ≥ ``1 - α`` for any ``n``.

    See Lei et al. (2018), Theorem 2.1.
    """
    s = sorted(float(x) for x in scores)
    n = len(s)
    if n == 0:
        return float("inf")
    if not (0.0 < alpha < 1.0):
        raise ValueError(f"alpha must be in (0, 1), got {alpha!r}")

    # Position of the conformal quantile (1-indexed). Clamp to [1, n].
    k = max(1, min(n, math.ceil((n + 1) * (1.0 - alpha))))
    return s[k - 1]


def empirical_coverage(
    scores: Iterable[float], alpha: float
) -> float:
    """Leave-one-out empirical coverage on a fixed score buffer.

    For each score ``s_i``, compute the conformal quantile from the *other*
    ``n-1`` scores at level ``α`` and check whether ``s_i`` falls below it.
    The fraction of ``i`` for which this holds is the LOO empirical coverage.
    """
    s = list(float(x) for x in scores)
    n = len(s)
    if n < 2:
        return float("nan")
    hits = 0
    for i in range(n):
        others = s[:i] + s[i + 1 :]
        q = quantile_with_finite_sample_correction(others, alpha)
        if s[i] <= q:
            hits += 1
    return hits / n


# ── High-level calibrator ──────────────────────────────────────────────


class SplitConformalCalibrator:
    """Online split-conformal calibrator with a rolling window.

    Usage::

        cal = SplitConformalCalibrator(bound=(0.0, 1.0), buf_size=500)
        cal.observe(prediction=0.20, realized=0.0)
        cal.observe(prediction=0.65, realized=1.0)
        # ... after enough observations ...
        ci = cal.interval(prediction=0.30, alpha=0.10)
        # ci.lower, ci.upper bracket the realized outcome with marginal
        # coverage ≥ 0.90 over the rolling window.
    """

    MIN_WARMUP_N = 30  # below this, intervals are "warm-up" wide.

    def __init__(
        self,
        *,
        bound: tuple[float | None, float | None] = (None, None),
        buf_size: int = 1000,
        state: CalibrationState | None = None,
    ) -> None:
        bl, bh = bound
        if bl is not None and bh is not None and bl >= bh:
            raise ValueError(f"bound_lo must be < bound_hi, got {bound!r}")
        if state is None:
            state = CalibrationState(
                buf_size=int(buf_size),
                bound_lo=bl,
                bound_hi=bh,
            )
        self.state = state

    # ── observe ──
    def observe(self, *, prediction: float, realized: float, ts: str | None = None) -> int:
        """Record a (prediction, realized) pair. Returns the new sample count."""
        score = nonconformity(prediction, realized)
        self.state.add(score)
        if ts:
            self.state.last_observation_ts = str(ts)
        return self.state.n

    # ── interval ──
    def interval(self, *, prediction: float, alpha: float = 0.1) -> ConformalInterval:
        n = self.state.n
        bl, bh = self.state.bound_lo, self.state.bound_hi

        if n < self.MIN_WARMUP_N:
            # During warm-up we still emit *something* so the desk gets
            # *some* sense of uncertainty. We use a generous half-width
            # equal to the bound range / 4 (or 0.5 if unbounded) — clearly
            # marked as warm-up so the UI can label it.
            if bl is not None and bh is not None:
                half = (bh - bl) / 4.0
            else:
                half = max(0.1, statistics.stdev(self.state.scores) if n >= 2 else 0.5)
            return _make_interval(
                point=prediction,
                half_width=half,
                alpha=alpha,
                n=n,
                warmup=True,
                bound_lo=bl,
                bound_hi=bh,
            )

        q = quantile_with_finite_sample_correction(self.state.scores, alpha)
        return _make_interval(
            point=prediction,
            half_width=q,
            alpha=alpha,
            n=n,
            warmup=False,
            bound_lo=bl,
            bound_hi=bh,
        )

    # ── coverage diagnostic ──
    def empirical_coverage(self, alpha: float = 0.1) -> float:
        return empirical_coverage(self.state.scores, alpha)


# ── Internal ───────────────────────────────────────────────────────────


def _make_interval(
    *,
    point: float,
    half_width: float,
    alpha: float,
    n: int,
    warmup: bool,
    bound_lo: float | None,
    bound_hi: float | None,
) -> ConformalInterval:
    lower = float(point) - float(half_width)
    upper = float(point) + float(half_width)
    if bound_lo is not None:
        lower = max(lower, bound_lo)
    if bound_hi is not None:
        upper = min(upper, bound_hi)
    # If the prediction itself is outside bounds (rare but possible for noisy
    # predictors), squash it back inside the bounds for the point so the UI
    # never plots out-of-domain values.
    if bound_lo is not None:
        point = max(point, bound_lo)
    if bound_hi is not None:
        point = min(point, bound_hi)
    return ConformalInterval(
        point=float(point),
        lower=float(lower),
        upper=float(upper),
        alpha=float(alpha),
        n_calibration=int(n),
        warmup=bool(warmup),
        bound_lo=bound_lo,
        bound_hi=bound_hi,
    )
