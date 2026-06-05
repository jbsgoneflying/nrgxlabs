"""Historical + live dealer-gamma factor series for Market Intelligence v2.

``dealer_gamma`` is the only HMM factor that can't be reconstructed from daily
prices — it needs the options chain. Historically it was zero-filled in
calibration, which made it a constant column: its per-state HMM emissions were
identical, so the feature cancelled out of the posterior and could never move
the regime label. This module sources a *consistent* raw signal for both
calibration (full history via ORATS ``hist/strikes``) and live serving (today
via ORATS live strikes), so train and serve never diverge.

Raw signal (per trading day):

    stress_raw = -callPutImbalance(SPX near-spot net GEX)

where ``callPutImbalance = (callsGex - putsGex) / (|callsGex| + |putsGex|)``.
Dealers SHORT gamma (net negative) → positive ``stress_raw``, matching the
factor convention "negative dealer gamma = stress". The HMM feature is the
rolling-252d z-score of ``stress_raw`` (the same ``_rolling_z`` every other
factor uses), so the units line up with the rest of the vector.

The raw series is cached (Redis + disk) keyed by trade date so a re-calibration
never re-fetches, and so the live path can z-score today's reading against the
exact trailing window the model was trained on.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.dealer_gamma_context import compute_dealer_gamma_context
from backend.market_intel.factors import FACTOR_KEYS, _rolling_z

LOG = logging.getLogger("market_intel.gamma_history")

# Index of dealer_gamma in the canonical factor vector.
DEALER_GAMMA_IDX = FACTOR_KEYS.index("dealer_gamma")

# ORATS index symbols for the broad-market dealer-gamma proxy (SPX, weeklies).
_GAMMA_SYMBOLS = ("SPX", "SPXW")

# Fields needed to compute strike-level gamma exposure near spot.
_STRIKE_FIELDS = (
    "ticker,tradeDate,expirDate,strike,spotPrice,stockPrice,"
    "gamma,callOpenInterest,putOpenInterest,callVolume,putVolume"
)

# Near-dated expiries carry the most dealer-relevant gamma.
_HIST_DTE = "3,21"

# Persistence keys.
_RAW_REDIS_KEY = "market_intel:dealer_gamma_raw:v1"


def _raw_disk_path() -> str:
    return os.getenv(
        "MI_DEALER_GAMMA_RAW_PATH",
        "data/market_intel_dealer_gamma_raw.json",
    )


# ---------------------------------------------------------------------------
# Raw signal
# ---------------------------------------------------------------------------


def stress_raw_from_rows(rows: List[dict]) -> Optional[float]:
    """Signed dealer-gamma stress from a strikes payload.

    Returns ``-callPutImbalance`` in roughly [-1, 1]; positive = net negative
    dealer gamma (destabilising / stress), negative = net positive (orderly).
    ``None`` when the chain is unusable.
    """
    if not rows:
        return None
    ctx = compute_dealer_gamma_context(rows)
    imbalance = ctx.get("callPutImbalance")
    if imbalance is None:
        return None
    try:
        return -float(imbalance)
    except (TypeError, ValueError):
        return None


def _rows_from_response(resp: Any) -> List[dict]:
    rows = getattr(resp, "rows", None)
    if isinstance(rows, list):
        return [r for r in rows if isinstance(r, dict)]
    return []


def fetch_stress_raw_for_date(orats_client: Any, trade_date: str) -> Optional[float]:
    """Historical dealer-gamma stress for one trade date via ORATS hist/strikes."""
    if orats_client is None:
        return None
    day = str(trade_date)[:10]
    for symbol in _GAMMA_SYMBOLS:
        try:
            resp = orats_client.hist_strikes(
                ticker=symbol,
                trade_date=day,
                fields=_STRIKE_FIELDS,
                dte=_HIST_DTE,
            )
        except Exception as e:  # noqa: BLE001 - tolerate per-date fetch failures
            LOG.debug("gamma_history: hist_strikes %s %s failed: %s", symbol, day, e)
            continue
        rows = _rows_from_response(resp)
        if len(rows) > 10:
            val = stress_raw_from_rows(rows)
            if val is not None:
                return val
    return None


def fetch_live_stress_raw(orats_client: Any, *, today: Optional[dt.date] = None) -> Optional[float]:
    """Today's dealer-gamma stress from live strikes (with EOD fallback)."""
    if orats_client is None:
        return None
    today = today or dt.date.today()

    # Live strikes, nearest weekly expiry first.
    days_until_friday = (4 - today.weekday()) % 7
    target_friday = today + dt.timedelta(days=days_until_friday or 7)
    for symbol in _GAMMA_SYMBOLS:
        try:
            resp = orats_client.live_strikes_by_expiry(
                ticker=symbol,
                expiry=target_friday.isoformat(),
                fields=_STRIKE_FIELDS,
            )
            rows = _rows_from_response(resp)
            if len(rows) > 10:
                val = stress_raw_from_rows(rows)
                if val is not None:
                    return val
        except Exception as e:  # noqa: BLE001
            LOG.debug("gamma_history: live_strikes %s failed: %s", symbol, e)

    # Fall back to the most recent historical close.
    return fetch_stress_raw_for_date(orats_client, today.isoformat())


# ---------------------------------------------------------------------------
# Persistence (Redis + disk), keyed by trade date
# ---------------------------------------------------------------------------


def load_raw_series(store: Any = None) -> Dict[str, float]:
    """Load the cached raw stress series (date → stress_raw), Redis then disk."""
    series: Dict[str, float] = {}

    # Disk first as the durable base.
    try:
        p = Path(_raw_disk_path())
        if p.exists():
            data = json.loads(p.read_text())
            if isinstance(data, dict):
                series.update({str(k)[:10]: float(v) for k, v in data.items()})
    except Exception as e:  # noqa: BLE001
        LOG.debug("gamma_history: disk load failed: %s", e)

    # Redis overlays / supplements disk.
    if store is not None:
        try:
            data = store.get_json(_RAW_REDIS_KEY)
            if isinstance(data, dict):
                series.update({str(k)[:10]: float(v) for k, v in data.items()})
        except Exception as e:  # noqa: BLE001
            LOG.debug("gamma_history: redis load failed: %s", e)

    return series


def save_raw_series(series: Dict[str, float], store: Any = None) -> None:
    """Persist the full raw stress series to disk and (if available) Redis."""
    clean = {str(k)[:10]: float(v) for k, v in series.items() if v is not None}
    try:
        p = Path(_raw_disk_path())
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(clean, indent=2, sort_keys=True))
    except Exception as e:  # noqa: BLE001
        LOG.warning("gamma_history: disk save failed: %s", e)
    if store is not None:
        try:
            store.set_json(_RAW_REDIS_KEY, clean, ttl_s=400 * 24 * 3600)
        except Exception as e:  # noqa: BLE001
            LOG.warning("gamma_history: redis save failed: %s", e)


def upsert_raw_point(date: str, value: float, store: Any = None) -> Dict[str, float]:
    """Idempotently add/update one date's raw value and persist."""
    series = load_raw_series(store)
    series[str(date)[:10]] = float(value)
    save_raw_series(series, store)
    return series


# ---------------------------------------------------------------------------
# History builder (calibration) + alignment
# ---------------------------------------------------------------------------


def build_raw_series(
    orats_client: Any,
    dates: List[str],
    *,
    store: Any = None,
    max_workers: int = 8,
    use_cache: bool = True,
) -> Dict[str, float]:
    """Build the raw stress series for ``dates``, fetching only the gaps.

    Cached dates are reused (so re-calibration is cheap); missing dates are
    fetched in parallel from ORATS, then the merged series is persisted.
    """
    cached = load_raw_series(store) if use_cache else {}
    wanted = [str(d)[:10] for d in dates]
    todo = [d for d in wanted if d not in cached]

    if todo and orats_client is not None:
        LOG.info(
            "gamma_history: fetching %d/%d dealer-gamma days (have %d cached)",
            len(todo), len(wanted), len(wanted) - len(todo),
        )
        fetched: Dict[str, float] = {}
        with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as ex:
            fut_to_date = {
                ex.submit(fetch_stress_raw_for_date, orats_client, d): d for d in todo
            }
            for fut in as_completed(fut_to_date):
                d = fut_to_date[fut]
                try:
                    v = fut.result()
                except Exception as e:  # noqa: BLE001
                    LOG.debug("gamma_history: fetch %s raised: %s", d, e)
                    v = None
                if v is not None:
                    fetched[d] = v
        if fetched:
            cached.update(fetched)
            save_raw_series(cached, store)

    return {d: cached[d] for d in wanted if d in cached}


def aligned_stress_series(series: Dict[str, float], dates: List[str]) -> List[float]:
    """Forward-fill the raw stress series onto ``dates`` (0.0 before first obs).

    Forward-fill (carry last) is correct for occasional chain gaps; we only fall
    back to 0.0 for the leading window before any reading exists.
    """
    out: List[float] = []
    last: Optional[float] = None
    for d in dates:
        key = str(d)[:10]
        if key in series:
            last = float(series[key])
        out.append(last if last is not None else 0.0)
    return out


def dealer_z_series(series: Dict[str, float], dates: List[str]) -> List[float]:
    """Rolling-252d z of the aligned stress series, in ``dates`` order.

    Mirrors the calibration ``_as_z_series`` construction so the live z and the
    trained feature share one definition.
    """
    raw = aligned_stress_series(series, dates)
    out = [0.0] * len(raw)
    for t in range(len(raw)):
        out[t] = _rolling_z(raw[: t + 1])
    return out


# ---------------------------------------------------------------------------
# Live serving
# ---------------------------------------------------------------------------


def dealer_z_today(
    orats_client: Any,
    *,
    store: Any = None,
    today: Optional[dt.date] = None,
) -> Optional[float]:
    """Compute today's dealer-gamma z against the persisted trailing window.

    Fetches today's raw stress, upserts it into the cached series, then returns
    the rolling z over the full date-sorted series (including today). ``None``
    when no live reading is available.
    """
    today = today or dt.date.today()
    raw_today = fetch_live_stress_raw(orats_client, today=today)
    if raw_today is None:
        return None
    series = upsert_raw_point(today.isoformat(), raw_today, store)
    ordered_dates = sorted(series.keys())
    raw_ordered = [series[d] for d in ordered_dates]
    return _rolling_z(raw_ordered)


# ---------------------------------------------------------------------------
# Model capability detection
# ---------------------------------------------------------------------------


def model_uses_dealer_gamma(model: Any) -> bool:
    """True if the model was actually trained on a live dealer-gamma feature.

    A zero-filled (placeholder) calibration leaves the dealer column constant:
    per-state means all 0 and stds pinned at the variance floor (~0.0316). A
    real z-scored feature has cross-state mean spread and std near 1. We use
    this to auto-gate live injection: with an old placeholder model we keep
    dealer_gamma at 0 (so it harmlessly cancels), and once a gamma-trained
    model is live, injection activates automatically.
    """
    try:
        means = model.emission_means
        stds = model.emission_stds
        idx = DEALER_GAMMA_IDX
        col_means = [float(means[s][idx]) for s in range(len(means))]
        col_stds = [float(stds[s][idx]) for s in range(len(stds))]
    except Exception:  # noqa: BLE001
        return False
    if not col_means or not col_stds:
        return False
    mean_spread = max(col_means) - min(col_means)
    max_std = max(col_stds)
    return mean_spread > 1e-3 or max_std > 0.1
