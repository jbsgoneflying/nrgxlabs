"""Shared Engine 1 breach-stats cache.

Engine 1 and Engine 15 both need the same core payload from
:func:`backend.earnings_logic.compute_breach_stats` plus the VRP /
width-comparison / entry-quality / desk-consensus enrichment. Before
this module existed, each engine had its own ``_run_engine1`` helper
and every scan + scenario + wing-console request re-ran ORATS bulk
fetches.

The desk's actual usage pattern:

1. Open E1 -> POST /api/breach for NVDA on 2026-05-28 AMC (warms pool).
2. Click "Simulate Top Pick" -> E15 opens with the same ticker +
   event; POST /api/earnings-ic/scenario fires within seconds.

Without a shared cache both steps pay full ORATS cost. With this
module the second step is a dict lookup.

Key design choices:

- Cache key includes ``event_date`` + ``event_timing`` so a manual
  override always busts the cache (same invariant as
  :func:`backend.deps.breach_cache_key`).
- 5 minute TTL balances freshness (intraday EM can drift) vs the
  scan-then-scenario cadence.
- The cache holds the **post-enrichment payload** (VRP, width,
  entry quality, desk consensus, em preference, goNoGo) so neither
  the E1 router nor the E15 simulator has to re-run the enrichment.
- Feeding optional ``trade_builder_inputs`` means E15 can shape the
  credit estimate to its chosen strikes when appropriate. The cache
  key includes a hash of the trade-builder shape so different
  placements don't collide.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import asdict
from typing import Any, Dict, Optional

from cachetools import TTLCache

from backend.config import FeatureFlags

LOG = logging.getLogger("engine1.shared_cache")


# 5-minute TTL is a compromise: long enough to span a desk's
# scan -> Wing Console -> Simulate-in-E15 journey (<60s typical),
# short enough that intraday EM drift or delayed-snapshot refreshes
# don't serve stale data for back-to-back desk runs.
_SHARED_CACHE: TTLCache = TTLCache(maxsize=2048, ttl=5 * 60)
_SHARED_CACHE_LOCK = threading.Lock()

# Separate stats so we can observe cache hit-rate in tests + prod.
_STATS = {"hits": 0, "misses": 0, "stores": 0, "busts": 0}
_STATS_LOCK = threading.Lock()


def _stats_bump(kind: str) -> None:
    with _STATS_LOCK:
        _STATS[kind] = int(_STATS.get(kind, 0)) + 1


def get_stats_snapshot() -> Dict[str, int]:
    """Return a copy of the current hit/miss counters."""
    with _STATS_LOCK:
        return dict(_STATS)


def reset_stats() -> None:
    """Zero the hit/miss counters (tests)."""
    with _STATS_LOCK:
        for k in list(_STATS.keys()):
            _STATS[k] = 0


def _fingerprint(obj: Any) -> str:
    """Stable short hash of an arbitrary JSON-serialisable object."""
    try:
        blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        blob = repr(obj)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _build_key(
    *,
    ticker:       str,
    n:            int,
    years:        int,
    k:            float,
    event_date:   Optional[str],
    event_timing: Optional[str],
    trade_builder_inputs: Optional[Dict[str, Any]],
    flags:        FeatureFlags,
) -> str:
    """Build the cache key. Same-ticker/event combos dedupe; different
    trade-builder shapes get different slots so the credit estimate
    doesn't bleed between wing-console placements."""
    ed = (event_date or "").strip()[:10]
    et = (event_timing or "").strip().upper()
    tb_fp = _fingerprint(trade_builder_inputs) if trade_builder_inputs else "none"
    flags_fp = _fingerprint(list(flags.cache_fingerprint())) if flags else "none"
    payload = f"{ticker.upper()}|{int(n)}|{int(years)}|{float(k):.3f}|{ed}|{et}|{tb_fp}|{flags_fp}"
    return payload


def clear() -> None:
    """Drop the entire cache (tests + admin only)."""
    with _SHARED_CACHE_LOCK:
        _SHARED_CACHE.clear()
    _stats_bump("busts")


def _enrich_payload(
    *,
    payload: Dict[str, Any],
    client:  Any,
    ticker:  str,
    flags:   FeatureFlags,
    benzinga_client: Any,
) -> Dict[str, Any]:
    """Run the VRP + width + entry-quality + desk-consensus pass on a
    fresh :func:`compute_breach_stats` payload. Mirrors the historic
    behaviour of ``_run_engine1`` in the E1 router + E15 simulator so
    both can just call :func:`get_or_compute_breach_stats` and trust
    the full shape.
    """
    from backend.e1_vrp_engine import (
        compute_e1_desk_consensus,
        compute_earnings_width_comparison,
        compute_em_preference,
        compute_entry_quality,
        compute_vrp_score,
    )
    from backend.go_no_go import compute_go_no_go

    try:
        payload["goNoGo"] = compute_go_no_go(
            client, ticker=ticker, payload=payload, benzinga_client=benzinga_client,
        )
    except Exception as err:
        LOG.debug("shared_cache: goNoGo failed for %s: %s", ticker, err)

    events = payload.get("events") or []
    current = payload.get("current") or {}
    current_em_pct: Optional[float] = None
    try:
        current_em_pct = float(current.get("impliedMovePct") or 0) or None
    except Exception:
        pass
    if current_em_pct is None:
        # Pre-market on announcement day: ORATS live /cores may return
        # null impErnMv. Fall back to the last-known delayed snapshot.
        try:
            d = current.get("delayedImpliedMovePct")
            if d is not None:
                f_d = float(d)
                if f_d > 0:
                    current_em_pct = f_d
        except Exception:
            pass

    try:
        vrp = compute_vrp_score(events, current_implied_move_pct=current_em_pct)
        payload["vrpAnalysis"] = vrp

        em_mults = [float(x.strip()) for x in str(flags.E1_EM_MULTS).split(",") if x.strip()]
        wing_pts = [float(x.strip()) for x in str(flags.E1_WING_WIDTH_PTS).split(",") if x.strip()]
        stock_price: Optional[float] = None
        try:
            stock_price = float(current.get("stockPrice") or 0) or None
        except Exception:
            pass

        wc, em_breach = compute_earnings_width_comparison(
            events, em_mults=em_mults, wing_pts=wing_pts,
            current_implied_move_pct=current_em_pct, stock_price=stock_price,
        )
        payload["widthComparison"] = wc
        payload["emBreachSummary"] = em_breach

        eq = compute_entry_quality(
            iv_elevation=vrp.get("ivElevation"),
            skew_overlay=payload.get("skewOverlay"),
            regime=payload.get("regime"),
            ticker_dealer_gamma=payload.get("tickerDealerGamma"),
            current=current,
            go_no_go=payload.get("goNoGo"),
        )
        payload["entryQuality"] = eq

        dc = compute_e1_desk_consensus(
            vrp=vrp, entry_quality=eq, em_breach_summary=em_breach,
            regime=payload.get("regime"), gap_vs_ctc=payload.get("gapVsCtc"),
            event_risk=payload.get("eventRisk"),
        )
        # v2: always compute but strip from external-facing /api/breach
        # based on E1_EMIT_DESK_CONSENSUS (router-level). Cache holds the
        # full enrichment so the E15 advisor + cross-engine consumers
        # can still access it.
        payload["deskConsensus"] = dc
        payload["emPreference"] = compute_em_preference(
            em_breach, vrp.get("vrpScore"), eq.get("entryQuality"),
        )
    except Exception as err:
        LOG.warning("shared_cache: VRP enrichment failed for %s: %s", ticker, err)

    return payload


def get_or_compute_breach_stats(
    *,
    ticker:       str,
    n:            int = 20,
    years:        int = 5,
    k:            float = 1.0,
    event_date:   Optional[str] = None,
    event_timing: Optional[str] = None,
    trade_builder_inputs: Optional[Dict[str, Any]] = None,
    client:       Any,
    benzinga_client: Any = None,
    flags:        FeatureFlags,
    force_refresh: bool = False,
    enrich:       bool = True,
) -> Dict[str, Any]:
    """Cache-first accessor for the enriched Engine 1 payload.

    Parameters mirror :func:`backend.earnings_logic.compute_breach_stats`
    plus the event override pair and an optional ``enrich`` flag (set
    ``False`` when the caller doesn't need VRP / width / desk consensus;
    currently only used by scripted probes).

    Cache keys include ``event_date`` / ``event_timing`` so an override
    always busts the prior slot. When ``force_refresh`` is true, the
    cache is bypassed and the new payload overwrites the slot on return.
    """
    from backend.earnings_logic import compute_breach_stats  # late import

    ticker = ticker.strip().upper()
    key = _build_key(
        ticker=ticker, n=n, years=years, k=k,
        event_date=event_date, event_timing=event_timing,
        trade_builder_inputs=trade_builder_inputs, flags=flags,
    )

    if not force_refresh:
        with _SHARED_CACHE_LOCK:
            hit = _SHARED_CACHE.get(key)
        if hit is not None:
            _stats_bump("hits")
            return hit

    _stats_bump("misses")
    next_override = None
    if event_date or event_timing:
        next_override = {
            "date":   event_date,
            "timing": (event_timing or "").upper() or None,
        }

    payload = compute_breach_stats(
        client=client, ticker=ticker, n=int(n), years=int(years), k=float(k),
        trade_builder_inputs=trade_builder_inputs,
        flags_override=flags,
        next_event_override=next_override,
        benzinga_client=benzinga_client,
    )
    if enrich:
        payload = _enrich_payload(
            payload=payload, client=client, ticker=ticker,
            flags=flags, benzinga_client=benzinga_client,
        )

    with _SHARED_CACHE_LOCK:
        _SHARED_CACHE[key] = payload
    _stats_bump("stores")
    return payload


__all__ = [
    "clear",
    "get_or_compute_breach_stats",
    "get_stats_snapshot",
    "reset_stats",
]
