"""Redis-backed persistence for conformal calibration state.

Each (engine, metric) pair gets its own Redis key:

    v2:conformal:{engine}:{metric}

Holding a JSON blob with the rolling buffer of nonconformity scores plus
the bounds and last-update timestamp. Scores are float lists — at the
default ``buf_size=1000`` that is ~8 kB per calibrator, easily small
enough to round-trip on every observe() call.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from .conformal import CalibrationState, SplitConformalCalibrator

LOG = logging.getLogger("v2.conformal_store")

KEY_PREFIX = "v2:conformal"
INDEX_KEY = "v2:conformal:index"  # set of "engine:metric" strings
DEFAULT_BUF_SIZE = 1000
DEFAULT_BOUNDS_BY_METRIC: dict[str, tuple[float | None, float | None]] = {
    # Probabilities: clip intervals to [0, 1].
    "breach_probability": (0.0, 1.0),
    "touch_probability": (0.0, 1.0),
    "outside_wings_probability": (0.0, 1.0),
    "win_rate": (0.0, 1.0),
    # Continuous outputs: unbounded.
    "p95_mae_pct": (0.0, None),
    "expected_pnl_dollars": (None, None),
    "expected_move_pct": (0.0, None),
    "credit_dollars": (None, None),
}


# ── Redis connection ──


def _redis_client():
    try:
        import redis  # type: ignore
    except ImportError:
        return None
    url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    try:
        return redis.from_url(url, decode_responses=True, socket_connect_timeout=2.0)
    except Exception as exc:
        LOG.warning("redis client init failed: %s", exc)
        return None


# ── Key helpers ──


def _key(engine: str, metric: str) -> str:
    e = (engine or "unknown").strip().lower()
    m = (metric or "unknown").strip().lower()
    return f"{KEY_PREFIX}:{e}:{m}"


def _normalize_pair(engine: str, metric: str) -> tuple[str, str]:
    return (engine or "unknown").strip().lower(), (metric or "unknown").strip().lower()


# ── Serialization ──


def _state_to_json(state: CalibrationState) -> str:
    return json.dumps(
        {
            "scores": [float(s) for s in state.scores],
            "buf_size": int(state.buf_size),
            "bound_lo": state.bound_lo,
            "bound_hi": state.bound_hi,
            "last_observation_ts": state.last_observation_ts,
        }
    )


def _state_from_json(blob: str | None) -> CalibrationState | None:
    if not blob:
        return None
    try:
        data: dict[str, Any] = json.loads(blob)
    except Exception:
        return None
    return CalibrationState(
        scores=[float(s) for s in (data.get("scores") or [])],
        buf_size=int(data.get("buf_size") or DEFAULT_BUF_SIZE),
        bound_lo=data.get("bound_lo"),
        bound_hi=data.get("bound_hi"),
        last_observation_ts=data.get("last_observation_ts"),
    )


# ── Public API ──


def load_calibrator(
    engine: str,
    metric: str,
    *,
    bound: tuple[float | None, float | None] | None = None,
    buf_size: int = DEFAULT_BUF_SIZE,
) -> SplitConformalCalibrator:
    """Load (or initialize) the calibrator for ``(engine, metric)``.

    If Redis is unavailable, returns a fresh in-memory calibrator. The
    returned object is *not* automatically persisted — callers should
    follow up with :func:`save_calibrator` after any ``observe`` call.
    """
    client = _redis_client()
    state: CalibrationState | None = None
    if client is not None:
        try:
            blob = client.get(_key(engine, metric))
            state = _state_from_json(blob)
        except Exception as exc:
            LOG.warning("conformal load failed for %s/%s: %s", engine, metric, exc)

    if state is None:
        bl, bh = bound if bound is not None else DEFAULT_BOUNDS_BY_METRIC.get(
            metric.lower(), (None, None)
        )
        state = CalibrationState(buf_size=int(buf_size), bound_lo=bl, bound_hi=bh)

    return SplitConformalCalibrator(state=state)


def save_calibrator(engine: str, metric: str, calibrator: SplitConformalCalibrator) -> bool:
    """Persist calibrator state. Returns True if landed on Redis."""
    client = _redis_client()
    if client is None:
        return False
    try:
        client.set(_key(engine, metric), _state_to_json(calibrator.state))
        client.sadd(INDEX_KEY, f"{(engine or 'unknown').lower()}:{(metric or 'unknown').lower()}")
        return True
    except Exception as exc:
        LOG.warning("conformal save failed for %s/%s: %s", engine, metric, exc)
        return False


def list_calibrators() -> list[dict[str, Any]]:
    """Enumerate persisted calibrators with their summary stats.

    Returns ``[]`` if Redis is unavailable. Each entry::

        {"engine": "e14", "metric": "breach_probability",
         "n": 87, "last_observation_ts": "2026-05-08T...",
         "bound": [0.0, 1.0]}
    """
    client = _redis_client()
    if client is None:
        return []
    try:
        keys = sorted(client.smembers(INDEX_KEY) or [])
    except Exception as exc:
        LOG.warning("conformal index read failed: %s", exc)
        return []

    out: list[dict[str, Any]] = []
    for k in keys:
        if ":" not in k:
            continue
        engine, metric = k.split(":", 1)
        cal = load_calibrator(engine, metric)
        out.append(
            {
                "engine": engine,
                "metric": metric,
                "n": cal.state.n,
                "buf_size": cal.state.buf_size,
                "bound": [cal.state.bound_lo, cal.state.bound_hi],
                "last_observation_ts": cal.state.last_observation_ts,
            }
        )
    return out


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
