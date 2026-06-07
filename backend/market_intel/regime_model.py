"""Pure-Python 3-state Gaussian HMM for market regime classification.

Implements:

- **Forward-backward smoothing** → posterior state probabilities P(s_t | x_1..T)
- **Baum-Welch training** (EM) to fit transition + emission parameters
- **State-ordering constraint** so state 0 = Risk-On, 1 = Transitional,
  2 = Stressed (ordered by mean composite z-score at fit time, making
  calibration deterministic across reruns)
- **Bootstrap confidence band** via parametric perturbation of the today
  factor vector against its own 1-sigma band

Why pure Python and not ``hmmlearn``?
  The Docker image for NRGX Labs deliberately avoids numpy/scikit for
  deployment size + determinism. 3 states × 8 factors × ~1300 days of
  history is well within stdlib performance (fit runs in <5s even under
  Python 3.12). All linear-algebra shortcuts assume **diagonal**
  covariance matrices — adequate for uncorrelated z-scored factors.

Schema for a calibrated model (JSON-serializable):

.. code:: json

    {
      "model_version": "mi_hmm_v1",
      "n_states": 3,
      "feature_keys": ["rv_spx_20d", ...],
      "state_labels": ["Risk-On", "Transitional", "Stressed"],
      "start_prob": [0.33, 0.34, 0.33],
      "trans_mat": [[0.90, 0.08, 0.02], ...],        # row-stochastic
      "emission_means": [[mu_f1, mu_f2, ...], ...],   # S × F
      "emission_stds":  [[sd_f1, sd_f2, ...], ...],   # S × F (diagonal)
      "calibrated_at": "2026-04-21T...",
      "training_days": 1260,
      "log_likelihood": -12345.67
    }
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import random
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.market_intel.factors import FACTOR_KEYS, FactorSnapshot

LOG = logging.getLogger("market_intel.regime_model")


MODEL_VERSION = "mi_hmm_v1"
STATE_LABELS: Tuple[str, str, str] = ("Risk-On", "Transitional", "Stressed")
N_STATES = 3

# EM convergence knobs.
_EM_MAX_ITER = 200
_EM_TOL = 1e-4

# Numerical floor for variance (prevent degeneracy on near-constant factors).
_VAR_FLOOR = 1e-3


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class CalibratedModel:
    """HMM parameters + metadata, JSON-round-trippable."""

    model_version:   str = MODEL_VERSION
    n_states:        int = N_STATES
    feature_keys:    List[str] = field(default_factory=lambda: list(FACTOR_KEYS))
    state_labels:    List[str] = field(default_factory=lambda: list(STATE_LABELS))
    start_prob:      List[float] = field(default_factory=lambda: [1.0 / N_STATES] * N_STATES)
    trans_mat:       List[List[float]] = field(default_factory=list)
    emission_means:  List[List[float]] = field(default_factory=list)
    emission_stds:   List[List[float]] = field(default_factory=list)
    calibrated_at:   str = ""
    training_days:   int = 0
    log_likelihood:  float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CalibratedModel":
        if not isinstance(d, dict):
            raise ValueError("CalibratedModel.from_dict: expected dict")
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass
class RegimeInference:
    """Result of running ``infer(model, factor_vector)``."""

    probs:                Dict[str, float] = field(default_factory=dict)   # state → prob
    label:                str = "Transitional"
    confidence:           float = 0.0       # max prob
    transition_risk_1d:   float = 0.0       # P(state_{t+1} more stressed | today)
    factor_contributions: Dict[str, float] = field(default_factory=dict)  # factor → log-lik delta
    anomaly_score:        float = 0.0       # heuristic 0-1, 0.0 = normal

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ConfidenceBand:
    """5th/50th/95th percentile per-state probabilities from bootstrap."""

    risk_on:      Dict[str, float] = field(default_factory=dict)
    transitional: Dict[str, float] = field(default_factory=dict)
    stressed:     Dict[str, float] = field(default_factory=dict)
    n_samples:    int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------


def _log_gaussian(x: float, mu: float, sigma: float) -> float:
    """Log-density of a 1-D Gaussian. Numerically safe."""
    sigma = max(sigma, math.sqrt(_VAR_FLOOR))
    z = (x - mu) / sigma
    return -0.5 * z * z - 0.5 * math.log(2.0 * math.pi) - math.log(sigma)


def _log_emission(
    x_vec: List[float],
    state_mus: List[float],
    state_sds: List[float],
) -> float:
    """Sum of per-factor log-Gaussians (diagonal covariance assumption)."""
    total = 0.0
    for xi, mu, sd in zip(x_vec, state_mus, state_sds):
        if math.isfinite(xi):
            total += _log_gaussian(xi, mu, sd)
    return total


def _logsumexp(values: List[float]) -> float:
    """Numerically stable log-sum-exp."""
    if not values:
        return -math.inf
    m = max(values)
    if m == -math.inf:
        return m
    return m + math.log(sum(math.exp(v - m) for v in values))


# ---------------------------------------------------------------------------
# Forward-backward (log-space)
# ---------------------------------------------------------------------------


def _forward_backward(
    observations: List[List[float]],
    start_log: List[float],
    trans_log: List[List[float]],
    means: List[List[float]],
    stds: List[List[float]],
) -> Tuple[List[List[float]], float]:
    """Return (posterior[t][s], total_log_likelihood)."""
    T = len(observations)
    S = len(start_log)
    if T == 0:
        return [], 0.0

    # Pre-compute log emissions.
    log_em = [
        [_log_emission(observations[t], means[s], stds[s]) for s in range(S)]
        for t in range(T)
    ]

    # Forward.
    alpha: List[List[float]] = [[0.0] * S for _ in range(T)]
    for s in range(S):
        alpha[0][s] = start_log[s] + log_em[0][s]
    for t in range(1, T):
        for s in range(S):
            alpha[t][s] = log_em[t][s] + _logsumexp(
                [alpha[t - 1][sp] + trans_log[sp][s] for sp in range(S)]
            )

    total_ll = _logsumexp(alpha[T - 1])

    # Backward.
    beta: List[List[float]] = [[0.0] * S for _ in range(T)]
    # beta[T-1] = 0 (log 1)
    for t in range(T - 2, -1, -1):
        for s in range(S):
            beta[t][s] = _logsumexp(
                [trans_log[s][sp] + log_em[t + 1][sp] + beta[t + 1][sp] for sp in range(S)]
            )

    # Posteriors.
    posterior: List[List[float]] = [[0.0] * S for _ in range(T)]
    for t in range(T):
        row = [alpha[t][s] + beta[t][s] for s in range(S)]
        z = _logsumexp(row)
        if z == -math.inf:
            for s in range(S):
                posterior[t][s] = 1.0 / S
        else:
            for s in range(S):
                posterior[t][s] = math.exp(row[s] - z)
    return posterior, total_ll


# ---------------------------------------------------------------------------
# Baum-Welch fit
# ---------------------------------------------------------------------------


def _initial_parameters(
    observations: List[List[float]],
    rng: random.Random,
) -> Tuple[List[float], List[List[float]], List[List[float]], List[List[float]]]:
    """Seed parameters using k-means-style partitioning by composite z.

    Composite-z = simple mean of factor z-values — because we want the
    three states seeded as "low avg stress" → "moderate" → "high avg
    stress" so that after EM the ordering stays stable.
    """
    T = len(observations)
    F = len(observations[0]) if T > 0 else len(FACTOR_KEYS)

    composite = [statistics.fmean(o) if o else 0.0 for o in observations]
    ranked = sorted(range(T), key=lambda i: composite[i])

    # Partition into 3 equal-sized groups.
    part = [ranked[:T // 3], ranked[T // 3:2 * T // 3], ranked[2 * T // 3:]]

    means: List[List[float]] = []
    stds:  List[List[float]] = []
    for group in part:
        if not group:
            means.append([0.0] * F)
            stds.append([1.0] * F)
            continue
        per_f_mu: List[float] = []
        per_f_sd: List[float] = []
        for f in range(F):
            vals = [observations[i][f] for i in group]
            mu = statistics.fmean(vals) if vals else 0.0
            try:
                sd = statistics.pstdev(vals) if len(vals) > 1 else 1.0
            except statistics.StatisticsError:
                sd = 1.0
            per_f_mu.append(mu + rng.uniform(-0.02, 0.02))  # tiny jitter for EM
            per_f_sd.append(max(sd, math.sqrt(_VAR_FLOOR)))
        means.append(per_f_mu)
        stds.append(per_f_sd)

    start = [1.0 / N_STATES] * N_STATES
    # Sticky transition matrix (states persist with p≈0.9, matches regime physics).
    trans = [[0.05] * N_STATES for _ in range(N_STATES)]
    for s in range(N_STATES):
        trans[s][s] = 0.90
    # Normalize.
    for s in range(N_STATES):
        row_sum = sum(trans[s])
        for sp in range(N_STATES):
            trans[s][sp] /= row_sum
    return start, trans, means, stds


def _xi_posterior(
    observations: List[List[float]],
    start: List[float],
    trans: List[List[float]],
    means: List[List[float]],
    stds: List[List[float]],
) -> Tuple[List[List[float]], List[List[List[float]]], float]:
    """Compute both (γ_t, ξ_{t,t+1}, log-likelihood) for the E-step."""
    T = len(observations)
    S = len(start)

    # Log versions to avoid underflow.
    start_log = [math.log(max(p, 1e-300)) for p in start]
    trans_log = [[math.log(max(p, 1e-300)) for p in row] for row in trans]

    # Forward / backward in log-space.
    log_em = [
        [_log_emission(observations[t], means[s], stds[s]) for s in range(S)]
        for t in range(T)
    ]
    alpha = [[0.0] * S for _ in range(T)]
    for s in range(S):
        alpha[0][s] = start_log[s] + log_em[0][s]
    for t in range(1, T):
        for s in range(S):
            alpha[t][s] = log_em[t][s] + _logsumexp(
                [alpha[t - 1][sp] + trans_log[sp][s] for sp in range(S)]
            )
    total_ll = _logsumexp(alpha[T - 1])

    beta = [[0.0] * S for _ in range(T)]
    for t in range(T - 2, -1, -1):
        for s in range(S):
            beta[t][s] = _logsumexp(
                [trans_log[s][sp] + log_em[t + 1][sp] + beta[t + 1][sp] for sp in range(S)]
            )

    # Gamma (posterior).
    gamma = [[0.0] * S for _ in range(T)]
    for t in range(T):
        row = [alpha[t][s] + beta[t][s] for s in range(S)]
        z = _logsumexp(row)
        for s in range(S):
            gamma[t][s] = math.exp(row[s] - z)

    # Xi (joint).
    xi: List[List[List[float]]] = [
        [[0.0] * S for _ in range(S)] for _ in range(T - 1)
    ]
    for t in range(T - 1):
        # Normalizer (over all (s, sp) pairs): alpha[t][s] + trans_log + log_em + beta = const.
        row: List[float] = []
        for s in range(S):
            for sp in range(S):
                row.append(alpha[t][s] + trans_log[s][sp] + log_em[t + 1][sp] + beta[t + 1][sp])
        z = _logsumexp(row)
        idx = 0
        for s in range(S):
            for sp in range(S):
                xi[t][s][sp] = math.exp(row[idx] - z)
                idx += 1

    return gamma, xi, total_ll


def fit_model(
    observations: List[List[float]],
    *,
    random_state: int = 1337,
    n_iter: int = _EM_MAX_ITER,
    tol: float = _EM_TOL,
) -> CalibratedModel:
    """Fit a 3-state Gaussian HMM via Baum-Welch.

    ``observations`` is a T × F matrix of z-scored factor values. Shorter
    series (< 200 days) return a default sticky-diagonal model with
    means = 0 and stds = 1 so consumers can still exercise the pipeline
    during cold-start.

    Post-fit, states are **re-ordered by emission-mean composite** so
    state 0 = Risk-On (lowest stress), 2 = Stressed (highest) — matching
    ``STATE_LABELS``. This guarantees deterministic downstream semantics.
    """
    T = len(observations)
    if T < 200:
        LOG.warning("market_intel.fit_model: only %d obs; returning sticky default", T)
        return _default_sticky_model(training_days=T)

    rng = random.Random(random_state)
    start, trans, means, stds = _initial_parameters(observations, rng)

    prev_ll = -math.inf
    total_ll = -math.inf
    for iteration in range(n_iter):
        gamma, xi, total_ll = _xi_posterior(observations, start, trans, means, stds)

        # M-step.
        # Start.
        start = [gamma[0][s] for s in range(N_STATES)]
        s_sum = sum(start)
        if s_sum > 0:
            start = [p / s_sum for p in start]

        # Transition matrix.
        new_trans = [[0.0] * N_STATES for _ in range(N_STATES)]
        for s in range(N_STATES):
            denom = sum(gamma[t][s] for t in range(T - 1))
            if denom <= 0:
                new_trans[s] = [1.0 / N_STATES] * N_STATES
                continue
            for sp in range(N_STATES):
                num = sum(xi[t][s][sp] for t in range(T - 1))
                new_trans[s][sp] = num / denom
        # Renormalize rows (guard against numerical drift).
        for s in range(N_STATES):
            rs = sum(new_trans[s])
            if rs > 0:
                new_trans[s] = [p / rs for p in new_trans[s]]
        trans = new_trans

        # Emissions.
        new_means = [[0.0] * len(observations[0]) for _ in range(N_STATES)]
        new_stds  = [[1.0] * len(observations[0]) for _ in range(N_STATES)]
        for s in range(N_STATES):
            denom = sum(gamma[t][s] for t in range(T))
            if denom <= 0:
                continue
            F = len(observations[0])
            for f in range(F):
                num_mu = sum(gamma[t][s] * observations[t][f] for t in range(T))
                mu = num_mu / denom
                new_means[s][f] = mu
                num_var = sum(gamma[t][s] * (observations[t][f] - mu) ** 2 for t in range(T))
                var = num_var / denom
                new_stds[s][f] = math.sqrt(max(var, _VAR_FLOOR))
        means = new_means
        stds  = new_stds

        if abs(total_ll - prev_ll) < tol:
            LOG.info("market_intel.fit_model converged at iter %d (ll=%.4f)", iteration + 1, total_ll)
            break
        prev_ll = total_ll

    # Re-order states by emission-mean composite (lowest → highest).
    composite_per_state = [statistics.fmean(means[s]) for s in range(N_STATES)]
    order = sorted(range(N_STATES), key=lambda s: composite_per_state[s])
    means = [means[i] for i in order]
    stds  = [stds[i]  for i in order]
    start = [start[i] for i in order]
    # Permute rows AND columns of the transition matrix.
    trans = [[trans[order[s]][order[sp]] for sp in range(N_STATES)] for s in range(N_STATES)]

    return CalibratedModel(
        model_version=MODEL_VERSION,
        n_states=N_STATES,
        feature_keys=list(FACTOR_KEYS),
        state_labels=list(STATE_LABELS),
        start_prob=start,
        trans_mat=trans,
        emission_means=means,
        emission_stds=stds,
        calibrated_at=dt.datetime.now(dt.timezone.utc).isoformat() + "Z",
        training_days=T,
        log_likelihood=float(total_ll),
    )


def _default_sticky_model(training_days: int = 0) -> CalibratedModel:
    """Cold-start fallback used when there's no calibration data yet."""
    F = len(FACTOR_KEYS)
    # Three states with means at -1, 0, +1 composite sigmas.
    means = [[-1.0] * F, [0.0] * F, [1.0] * F]
    stds  = [[1.0]  * F, [1.0] * F, [1.0] * F]
    trans = [
        [0.90, 0.08, 0.02],
        [0.06, 0.88, 0.06],
        [0.02, 0.08, 0.90],
    ]
    return CalibratedModel(
        model_version=MODEL_VERSION,
        n_states=N_STATES,
        feature_keys=list(FACTOR_KEYS),
        state_labels=list(STATE_LABELS),
        start_prob=[1.0 / N_STATES] * N_STATES,
        trans_mat=trans,
        emission_means=means,
        emission_stds=stds,
        calibrated_at=dt.datetime.now(dt.timezone.utc).isoformat() + "Z",
        training_days=training_days,
        log_likelihood=0.0,
    )


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


def _stationary_distribution(trans_mat: List[List[float]], n_iter: int = 200) -> List[float]:
    """Compute the stationary distribution of a row-stochastic matrix
    by power iteration. Returns a probability vector ``π`` such that
    ``π = π × P``. Robust to any 3-state matrix.
    """
    S = len(trans_mat)
    pi = [1.0 / S] * S
    for _ in range(n_iter):
        new = [0.0] * S
        for sp in range(S):
            new[sp] = sum(pi[s] * trans_mat[s][sp] for s in range(S))
        # Normalize (guard against numeric drift).
        total = sum(new)
        if total > 0:
            new = [x / total for x in new]
        # Convergence check.
        if all(abs(new[s] - pi[s]) < 1e-9 for s in range(S)):
            return new
        pi = new
    return pi


def infer(
    model: CalibratedModel,
    today_vector: List[float],
) -> RegimeInference:
    """Single-day posterior using the **stationary distribution** of the
    HMM transition matrix as the prior, not the training-set start_prob
    (which is concentrated on whichever state the calibration window
    happened to begin in). Posterior is proportional to π(s) × p(x | s).
    """
    S = model.n_states

    # Stationary prior — uniform fallback if the trans matrix is degenerate.
    try:
        prior = _stationary_distribution(model.trans_mat)
        if not all(math.isfinite(p) and p > 0 for p in prior):
            prior = [1.0 / S] * S
    except Exception:
        prior = [1.0 / S] * S

    log_em = [
        _log_emission(today_vector, model.emission_means[s], model.emission_stds[s])
        for s in range(S)
    ]
    log_prior = [math.log(max(p, 1e-300)) for p in prior]
    log_post = [log_prior[s] + log_em[s] for s in range(S)]
    z = _logsumexp(log_post)
    probs = [math.exp(lp - z) for lp in log_post]

    label_idx = max(range(S), key=lambda s: probs[s])
    label = model.state_labels[label_idx]
    confidence = probs[label_idx]

    # Transition risk: P(t+1 is MORE-stressed than today) via transition matrix.
    # "More stressed" = any state index > current label_idx.
    transition_risk_1d = 0.0
    for s in range(S):
        for sp in range(S):
            if sp > s and probs[s] > 0:
                transition_risk_1d += probs[s] * model.trans_mat[s][sp]

    # Factor contributions: how much each factor pushed the classification
    # toward the max state vs the average state. A factor whose value is
    # far from ALL state means gets a small contribution; one that's
    # diagnostic (close to winner, far from losers) gets a large one.
    contributions: Dict[str, float] = {}
    for f, key in enumerate(FACTOR_KEYS):
        per_state = [
            _log_gaussian(today_vector[f], model.emission_means[s][f], model.emission_stds[s][f])
            for s in range(S)
        ]
        # Delta of winner from the MEAN of losers.
        winner = per_state[label_idx]
        losers = [per_state[s] for s in range(S) if s != label_idx]
        mean_loser = statistics.fmean(losers) if losers else winner
        contributions[key] = round(winner - mean_loser, 3)

    # Heuristic anomaly score: max Mahalanobis-like distance across states.
    worst_distance = 0.0
    for s in range(S):
        for f, v in enumerate(today_vector):
            mu = model.emission_means[s][f]
            sd = max(model.emission_stds[s][f], math.sqrt(_VAR_FLOOR))
            dist = abs((v - mu) / sd)
            worst_distance = max(worst_distance, dist)
    # 3.0+ sigma = 0.05 p-value (two-sided) ballpark; scale to [0, 1].
    anomaly_score = min(1.0, max(0.0, (worst_distance - 2.0) / 3.0))

    return RegimeInference(
        probs={
            "risk_on":      round(probs[0], 4),
            "transitional": round(probs[1], 4),
            "stressed":     round(probs[2], 4),
        },
        label=label,
        confidence=round(confidence, 4),
        transition_risk_1d=round(transition_risk_1d, 4),
        factor_contributions=contributions,
        anomaly_score=round(anomaly_score, 3),
    )


# ---------------------------------------------------------------------------
# Bootstrap confidence band
# ---------------------------------------------------------------------------


def bootstrap_confidence(
    model: CalibratedModel,
    today_vector: List[float],
    *,
    n_samples: int = 500,
    sigma_frac: float = 0.15,
    random_state: int = 42,
) -> ConfidenceBand:
    """Perturb each factor by its 1-sigma × ``sigma_frac`` and re-infer.

    Returns the 5/50/95 percentiles of P(state) across samples.
    """
    rng = random.Random(random_state)
    S = model.n_states
    samples = {s: [] for s in range(S)}

    # Per-factor 1σ reference (average across states — diagonal model).
    per_f_sigma = [
        statistics.fmean(model.emission_stds[s][f] for s in range(S))
        for f in range(len(today_vector))
    ]
    for _ in range(n_samples):
        perturbed = [
            today_vector[f] + rng.gauss(0.0, sigma_frac * per_f_sigma[f])
            for f in range(len(today_vector))
        ]
        inf_res = infer(model, perturbed)
        samples[0].append(inf_res.probs["risk_on"])
        samples[1].append(inf_res.probs["transitional"])
        samples[2].append(inf_res.probs["stressed"])

    def _pcts(xs: List[float]) -> Dict[str, float]:
        if not xs:
            return {"p5": 0.0, "p50": 0.0, "p95": 0.0}
        xs_sorted = sorted(xs)
        n = len(xs_sorted)
        return {
            "p5":  round(xs_sorted[max(0, int(0.05 * n))], 4),
            "p50": round(xs_sorted[n // 2], 4),
            "p95": round(xs_sorted[min(n - 1, int(0.95 * n))], 4),
        }

    return ConfidenceBand(
        risk_on=_pcts(samples[0]),
        transitional=_pcts(samples[1]),
        stressed=_pcts(samples[2]),
        n_samples=n_samples,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_model(model: CalibratedModel, path: str) -> bool:
    """Persist a calibrated model to a JSON file."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(model.to_dict(), indent=2, default=str))
        return True
    except Exception as e:
        LOG.error("market_intel.save_model failed: %s", e)
        return False


def load_model(path: str) -> Optional[CalibratedModel]:
    """Load a calibrated model from JSON. Returns None on failure."""
    try:
        p = Path(path)
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        return CalibratedModel.from_dict(data)
    except Exception as e:
        LOG.warning("market_intel.load_model failed (%s): %s", path, e)
        return None


def model_from_redis(store: Any, key: str) -> Optional[CalibratedModel]:
    if store is None:
        return None
    try:
        data = store.get_json(key)
        if not data:
            return None
        return CalibratedModel.from_dict(data)
    except Exception as e:
        LOG.warning("market_intel.model_from_redis failed (%s): %s", key, e)
        return None


def model_to_redis(store: Any, key: str, model: CalibratedModel, ttl_s: int = 604800) -> bool:
    if store is None:
        return False
    try:
        return bool(store.set_json(key, model.to_dict(), ttl_s=ttl_s))
    except Exception as e:
        LOG.error("market_intel.model_to_redis failed: %s", e)
        return False
