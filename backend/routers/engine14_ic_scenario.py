"""Engine 14 — IC Scenario Simulator routes."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import logging
import os
import threading
from typing import Any, Dict, Optional

from cachetools import TTLCache
from fastapi import APIRouter, Body, Header, HTTPException, Query

from backend.config import get_flags
from backend.deps import get_benzinga_client_optional, get_client
from backend.engine14 import chain_cache, reconciliation, regime_features
from backend.engine14.conditioning import load_modifier_coefficients
from backend.engine14.live_chain import fetch_live_chain_nbbo, validate_strikes_exist
from backend.engine14.simulator import IcScenarioRequest, run_scenario
from backend.engine2_trades import get_trade, log_trade
from backend.redis_store import get_store_optional
from backend.spx_ic import compute_engine2_spx_ic

LOG = logging.getLogger("engine14.router")

router = APIRouter()

# Small request-level cache so repeated identical submissions (same request body)
# return in milliseconds rather than re-doing the replay loop.
_scenario_cache_lock = threading.Lock()
_scenario_cache: TTLCache = TTLCache(maxsize=512, ttl=10 * 60)


def _ensure_enabled() -> None:
    f = get_flags()
    if not getattr(f, "ENABLE_ENGINE14_IC_SCENARIO", False):
        raise HTTPException(status_code=404, detail="Engine 14 disabled (ENABLE_ENGINE14_IC_SCENARIO=0).")


def _parse_request(body: Dict[str, Any]) -> IcScenarioRequest:
    def _req_float(k: str) -> float:
        if k not in body or body[k] is None:
            raise HTTPException(status_code=400, detail=f"Missing required field: {k}")
        try:
            return float(body[k])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Field {k} must be numeric.")

    def _req_str(k: str) -> str:
        if k not in body or not body[k]:
            raise HTTPException(status_code=400, detail=f"Missing required field: {k}")
        return str(body[k]).strip()

    underlying = str(body.get("underlying") or "SPX").upper()
    if underlying != "SPX":
        raise HTTPException(status_code=400, detail="Engine 14 supports SPX only in Phase 1.")

    f = get_flags()
    try:
        req = IcScenarioRequest(
            underlying=underlying,
            entry_date=_req_str("entryDate"),
            expiry=_req_str("expiry"),
            short_put=_req_float("shortPut"),
            long_put=_req_float("longPut"),
            short_call=_req_float("shortCall"),
            long_call=_req_float("longCall"),
            credit_received=_req_float("creditReceived"),
            profit_target_pct=float(body.get("profitTargetPct", f.ENGINE14_DEFAULT_PROFIT_TARGET_PCT)),
            stop_loss_pct=float(body.get("stopLossPct", f.ENGINE14_DEFAULT_STOP_LOSS_PCT)),
            season_mode=str(body.get("seasonMode") or "none"),
            season_value=(str(body.get("seasonValue")).strip() if body.get("seasonValue") else None),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid request: {type(e).__name__}: {e}")

    # Sanity checks.
    if not (req.long_put < req.short_put < req.short_call < req.long_call):
        raise HTTPException(
            status_code=400,
            detail="Strikes must satisfy: longPut < shortPut < shortCall < longCall.",
        )
    if req.credit_received <= 0:
        raise HTTPException(status_code=400, detail="creditReceived must be positive.")
    if req.entry_date >= req.expiry:
        raise HTTPException(status_code=400, detail="expiry must be after entryDate.")
    return req


def _cache_key(req: IcScenarioRequest) -> tuple:
    f = get_flags()
    return (
        req.underlying, req.entry_date, req.expiry,
        req.short_put, req.long_put, req.short_call, req.long_call,
        round(float(req.credit_received), 4),
        float(req.profit_target_pct), float(req.stop_loss_pct),
        req.season_mode, req.season_value,
        f.cache_key_engine14(),
    )


@router.post("/api/ic-scenario")
def ic_scenario(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    _ensure_enabled()
    req = _parse_request(body)
    key = _cache_key(req)

    with _scenario_cache_lock:
        cached = _scenario_cache.get(key)
    if cached is not None:
        return cached

    try:
        client = get_client()
    except Exception as e:
        LOG.exception("engine14: ORATS client init failed")
        raise HTTPException(status_code=503, detail=f"ORATS client unavailable: {e}")

    try:
        bz = get_benzinga_client_optional()
    except Exception:
        bz = None
    try:
        store = get_store_optional()
    except Exception:
        store = None

    try:
        result = run_scenario(req, client=client, benzinga_client=bz, store=store)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        LOG.exception("engine14: run_scenario failed")
        raise HTTPException(status_code=500, detail=f"Scenario replay failed: {type(e).__name__}: {e}")

    # v2 response augmentation — Command Deck handoff fields + MI v2
    # source chip + optional legacy-verdict strip.
    f_v2 = get_flags()
    result = _augment_scenario_v2(result, body=body, flags=f_v2)

    with _scenario_cache_lock:
        _scenario_cache[key] = result
    return result


# ---------------------------------------------------------------------------
# v2 response augmentation
# ---------------------------------------------------------------------------


def _augment_scenario_v2(
    result: Dict[str, Any], *, body: Dict[str, Any], flags: Any,
) -> Dict[str, Any]:
    """Add Command Deck fields + strip legacy verdict when flag is off.

    Fields added:

    - ``wingConsoleCacheKey`` / ``placementRank`` — echoed from the
      request body so the frontend can deep-link back to the Wing
      Console card that produced the selected placement.
    - ``mcResults`` — empty unless the scenario was entered via the
      Wing Console handoff (the full MC distribution is already on
      the Wing Console response). This keeps /api/ic-scenario
      backward-compatible while letting the Command Deck render the
      MC reading without a second call.
    - ``sourceChip`` — ``desk_default`` unless the request body carried
      a ``sourceChip`` override (matches the E2 / E15 chip semantics).

    Strip (when ``E14_EMIT_DESK_CONSENSUS=False``):

    - ``reconcile.overall`` (top-level verdict string only — per-chip
      detail stays).
    - Echoed ``engine2.deskConsensus`` / ``engine2.recSimple`` if the
      scenario payload was chained through /reconcile.
    """
    try:
        result["wingConsoleCacheKey"] = body.get("wingConsoleCacheKey") or None
        result["placementRank"] = (
            int(body["placementRank"])
            if body.get("placementRank") is not None else None
        )
    except Exception:
        result["wingConsoleCacheKey"] = None
        result["placementRank"] = None

    src_chip = str(body.get("sourceChip") or "").strip().lower()
    if src_chip not in ("desk_default", "user_override"):
        src_chip = "desk_default"
    result["sourceChip"] = src_chip

    if "mcResults" not in result:
        result["mcResults"] = {}

    if bool(getattr(flags, "ENABLE_E14_V2", False)) and not bool(
        getattr(flags, "E14_EMIT_DESK_CONSENSUS", False)
    ):
        recon = result.get("reconcile")
        if isinstance(recon, dict) and "overall" in recon:
            # Preserve per-chip detail, strip just the top-level verdict.
            recon.pop("overall", None)
        e2 = result.get("engine2")
        if isinstance(e2, dict):
            e2.pop("deskConsensus", None)
            e2.pop("recSimple", None)
    return result


# ---------------------------------------------------------------------------
# v2 Wing Decision Console + exact-slider scoring
# ---------------------------------------------------------------------------


def _ensure_v2_enabled() -> None:
    f = get_flags()
    if not bool(getattr(f, "ENABLE_E14_V2", False)):
        raise HTTPException(status_code=404, detail="Engine 14 v2 disabled (ENABLE_E14_V2=0).")
    _ensure_enabled()


def _parse_wing_console_body(body: Dict[str, Any]) -> Dict[str, Any]:
    underlying = str(body.get("underlying") or "SPX").upper()
    if underlying != "SPX":
        raise HTTPException(status_code=400, detail="Engine 14 supports SPX only in Phase 1.")
    ed = str(body.get("entry_date") or body.get("entryDate") or "").strip()[:10]
    xp = str(body.get("expiry_date") or body.get("expiry") or body.get("expiryDate") or "").strip()[:10]
    if not ed or not xp:
        raise HTTPException(status_code=400, detail="entry_date and expiry_date are required.")
    try:
        a = dt.date.fromisoformat(ed)
        b = dt.date.fromisoformat(xp)
    except Exception as _e:
        raise HTTPException(status_code=400, detail=f"entry_date/expiry_date must be YYYY-MM-DD: {_e}") from _e
    if b <= a:
        raise HTTPException(status_code=400, detail="expiry_date must be after entry_date.")
    return {"underlying": underlying, "entry_date": ed, "expiry_date": xp}


def _build_e14_wing_console_payload(
    *,
    underlying:  str,
    entry_date:  str,
    expiry_date: str,
    flags:       Any,
    weights:     Any,
) -> Dict[str, Any]:
    """Run the full Wing Console pipeline for E14.

    Loads SPX bars, builds the analogue universe, filters to the
    user's entry-day regime, then hands off to
    :func:`backend.engine14.wing_console.build_wing_console` for
    scoring.
    """
    from backend.engine14 import (
        build_wing_console as _build_wing_console,
    )
    from backend.engine14.analogue_matcher import (
        MatchCriteria,
        build_analogue_universe,
        filter_analogues,
    )
    from backend.engine14.simulator import (
        _infer_user_em_pct,
        _resolve_entry_regime,
        IcScenarioRequest,
    )
    from backend.spx_ic.ohlc import DailyOHLC, fetch_dailies_ohlc_range

    client = get_client()
    try:
        store = get_store_optional()
    except Exception:
        store = None

    today = dt.date.today()
    lookback_start = today - dt.timedelta(days=int(flags.ENGINE14_LOOKBACK_YEARS) * 370)
    bars = fetch_dailies_ohlc_range(
        client, ticker=underlying, start=lookback_start, end=today,
    )
    closes_sorted = [
        (b.trade_date, float(b.close)) for b in bars if b.close is not None
    ]
    closes_sorted.sort(key=lambda x: x[0])
    closes_by_date = {d: c for d, c in closes_sorted}
    ohlc_by_date: Dict[str, DailyOHLC] = {
        b.trade_date: b for b in bars if b is not None
    }
    if len(closes_sorted) < 180:
        raise HTTPException(
            status_code=503,
            detail="Insufficient SPX history loaded (need at least ~9 months of bars).",
        )

    # Minimal request proxy so _infer_user_em_pct + _resolve_entry_regime
    # reuse the existing helpers. Strikes/credit are placeholders — the
    # Wing Console doesn't need them to derive EM + regime.
    pseudo_req = IcScenarioRequest(
        underlying=underlying,
        entry_date=entry_date,
        expiry=expiry_date,
        short_put=0.0, long_put=0.0, short_call=0.0, long_call=0.0,
        credit_received=1.0,
    )
    user_spot, user_em_pct, em_source, user_spot_as_of = _infer_user_em_pct(
        pseudo_req, closes_by_date,
    )
    resolved_regime = _resolve_entry_regime(
        user_em_pct=user_em_pct, request=pseudo_req, store=store, flags=flags,
    )
    user_regime = resolved_regime.bucket

    # Capture the MI v2 snapshot (same shape as simulator.py v2 overlay).
    regime_mi_v2: Optional[Dict[str, Any]] = None
    try:
        if bool(getattr(flags, "ENABLE_MI_V2", False)):
            from backend.market_intel import regime_snapshot as _mi_snap
            _mi = _mi_snap()
            if _mi is not None:
                _probs = getattr(_mi, "probabilities", None) or {}
                regime_mi_v2 = {
                    "label":         str(getattr(_mi, "label", "") or "") or None,
                    "probabilities": dict(_probs) if isinstance(_probs, dict) else {},
                    "vol_state":     getattr(_mi, "vol_state", None),
                    "source":        getattr(_mi, "source", "v2_hmm"),
                }
    except Exception:
        regime_mi_v2 = None

    # Build + filter analogue universe (no strike-map step — we just need
    # the pool for MAE + MC + historical breach fallback).
    entry_dow = 0
    target_dte_calendar = 4
    try:
        entry_dow = dt.date.fromisoformat(entry_date).weekday()
        if entry_dow > 4:
            entry_dow = 2
        target_dte_calendar = max(
            1,
            (dt.date.fromisoformat(expiry_date) - dt.date.fromisoformat(entry_date)).days,
        )
    except Exception:
        pass

    from backend.engine14.simulator import _count_trading_sessions
    target_dte_sessions = _count_trading_sessions(
        entry_date, expiry_date, closes_by_date, flags=flags,
    )

    universe = build_analogue_universe(
        ticker=underlying,
        closes_sorted=closes_sorted,
        entry_dow=entry_dow,
        target_dte_calendar_days=target_dte_calendar,
        max_windows=int(flags.ENGINE14_MAX_ANALOGUES),
    )
    criteria = MatchCriteria(
        target_regime=user_regime,
        target_dte_sessions=target_dte_sessions,
        regime_bucket_tol=float(flags.ENGINE14_REGIME_BUCKET_TOL),
        season_mode="none",
        season_value=None,
        em_multiple_tol=float(flags.ENGINE14_EM_MULTIPLE_TOL),
        enable_em_multiple_filter=bool(flags.ENGINE14_ENABLE_EM_MULTIPLE_FILTER),
        enable_knn_regime=bool(getattr(flags, "ENGINE14_ENABLE_KNN_REGIME", False)),
        knn_top_n=int(getattr(flags, "ENGINE14_KNN_TOP_N", 80)),
    )
    try:
        candidates = filter_analogues(universe=universe, criteria=criteria, flags=flags)
    except TypeError:
        # Older signatures accepted positional only.
        candidates = filter_analogues(universe, criteria=criteria, flags=flags)

    # Translate AnalogueWindow -> dict pool for the scorer.
    pool: List[Dict[str, Any]] = []
    for w in candidates:
        pool.append({
            "entry_date":   getattr(w, "entry_date", None),
            "expiry_date":  getattr(w, "expiry_date", None),
            "regime_bucket": getattr(w, "regime_bucket", None) or user_regime,
            "macro_bucket": "NORMAL",
            "entry_close":  getattr(w, "entry_close", None),
        })

    macro_bucket = "NORMAL"
    hold_days = max(1, target_dte_sessions)

    payload, mae_dist, mc_result = _build_wing_console(
        entry_date=entry_date,
        expiry_date=expiry_date,
        as_of_date=today.isoformat(),
        spot=float(user_spot or 0.0),
        em_pct=float(user_em_pct or 0.0),
        hold_days=int(hold_days),
        dte_calendar_days=int(target_dte_calendar),
        analogue_pool=pool,
        closes_by_date=closes_by_date,
        ohlc_by_date=ohlc_by_date,
        regime_label=user_regime,
        regime_bucket=user_regime,
        regime_mi_v2=regime_mi_v2,
        macro_bucket=macro_bucket,
        weights=weights,
        flags=flags,
    )

    return {
        "schemaVersion":    1,
        "wingConsole":      payload.to_dict(),
        "mcResults":        (mc_result.to_dict() if mc_result else {}),
        "maeDistribution":  mae_dist.to_dict(),
        "regime":           {"mi_v2": regime_mi_v2},
        "entryState": {
            "userSpot":       round(float(user_spot or 0.0), 2),
            "userSpotAsOf":   user_spot_as_of,
            "userSpotIsLive": bool(user_spot_as_of == entry_date),
            "userEmPct":      round(float(user_em_pct or 0.0), 3),
            "regimeBucket":   user_regime,
            "regimeSource":   resolved_regime.source,
            "regimeSourceAsOf": resolved_regime.as_of,
            "regimeMiV2":     regime_mi_v2,
        },
    }


# Cache for wing-console payloads keyed on (entry, expiry, as_of, weights, flags).
_wc_cache_lock = threading.Lock()
_wc_cache: TTLCache = TTLCache(maxsize=256, ttl=10 * 60)


def _wc_cache_key(
    *,
    underlying: str, entry_date: str, expiry_date: str,
    weights: Dict[str, float], flags: Any,
) -> tuple:
    import hashlib
    import json as _json
    wf = hashlib.sha256(
        _json.dumps(weights or {}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:10]
    return (underlying, entry_date, expiry_date, wf, flags.cache_key_engine14())


@router.post("/api/ic-scenario/wing-console")
def ic_scenario_wing_console(body: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """v2 ranked placement grid. Returns wingConsole + mcResults +
    maeDistribution + regime.mi_v2. Cached 10 min on
    (underlying, entry_date, expiry_date, weights_fp, flags_fp).
    """
    _ensure_v2_enabled()
    parsed = _parse_wing_console_body(body)

    f = get_flags()
    from backend.engine14.wing_console import WingConsoleWeights
    weights = WingConsoleWeights.from_flags(f)
    wopts = body.get("weights") or {}
    if isinstance(wopts, dict):
        for k_, v_ in wopts.items():
            # Support both "breach" (plan) and "close" (E2 alias).
            target = k_
            if k_ == "breach":
                target = "close"
            if hasattr(weights, target):
                try:
                    setattr(weights, target, float(v_))
                except Exception:
                    pass

    key = _wc_cache_key(
        underlying=parsed["underlying"], entry_date=parsed["entry_date"],
        expiry_date=parsed["expiry_date"], weights=weights.as_dict(), flags=f,
    )
    with _wc_cache_lock:
        hit = _wc_cache.get(key)
    if hit is not None:
        return hit

    try:
        payload = _build_e14_wing_console_payload(
            underlying=parsed["underlying"],
            entry_date=parsed["entry_date"],
            expiry_date=parsed["expiry_date"],
            flags=f,
            weights=weights,
        )
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("engine14 wing-console failed")
        raise HTTPException(
            status_code=500,
            detail=f"Wing Console failed: {type(e).__name__}: {e}",
        ) from e

    payload["updatedAt"] = dt.datetime.utcnow().isoformat() + "Z"
    payload["weightsUsed"] = weights.as_dict()
    with _wc_cache_lock:
        _wc_cache[key] = payload
    return payload


@router.post("/api/ic-scenario/wing-console/score-placement")
def ic_scenario_score_placement(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """v2 exact-slider scoring against a cached ScoringContext.

    Cold-start path rebuilds the context by running the full Wing
    Console once so the slider still works on first click.
    """
    _ensure_v2_enabled()

    underlying = str(body.get("underlying") or "SPX").strip().upper()
    entry_date = str(body.get("entry_date") or body.get("entryDate") or "")[:10]
    expiry_date = str(body.get("expiry_date") or body.get("expiry") or body.get("expiryDate") or "")[:10]
    as_of_date = str(body.get("as_of_date") or "").strip()[:10] or dt.date.today().isoformat()
    try:
        em_mult = float(body["em_mult"])
        wing_pts = float(body["wing_pts"])
    except (TypeError, KeyError, ValueError) as _e:
        raise HTTPException(status_code=400, detail="em_mult and wing_pts must be numeric") from _e
    if not (0.25 <= em_mult <= 3.0):
        raise HTTPException(status_code=400, detail="em_mult out of range [0.25, 3.0]")
    if not (0.5 <= wing_pts <= 100.0):
        raise HTTPException(status_code=400, detail="wing_pts out of range [0.5, 100.0]")

    f = get_flags()
    from backend.engine14 import (
        WingConsoleWeights,
        get_scoring_context,
        score_single_placement,
    )

    weights = WingConsoleWeights.from_flags(f)
    wopts = body.get("weights") or {}
    if isinstance(wopts, dict):
        for k_, v_ in wopts.items():
            target = "close" if k_ == "breach" else k_
            if hasattr(weights, target):
                try:
                    setattr(weights, target, float(v_))
                except Exception:
                    pass

    refresh = bool(body.get("refresh"))
    ctx = None if refresh else get_scoring_context(entry_date, expiry_date, as_of_date)
    source = "cached_context"
    if ctx is None:
        try:
            _build_e14_wing_console_payload(
                underlying=underlying,
                entry_date=entry_date,
                expiry_date=expiry_date,
                flags=f,
                weights=weights,
            )
            ctx = get_scoring_context(entry_date, expiry_date, as_of_date)
            if ctx is None:
                # Cold start may have published under today.iso if caller left
                # as_of blank; try that.
                today_iso = dt.date.today().isoformat()
                ctx = get_scoring_context(entry_date, expiry_date, today_iso)
            source = "rebuilt_context"
        except HTTPException:
            raise
        except Exception as e:
            LOG.exception("engine14 score-placement cold start failed")
            raise HTTPException(
                status_code=500,
                detail=f"Cold start failed: {type(e).__name__}: {e}",
            ) from e

    if ctx is None:
        raise HTTPException(status_code=500, detail="Unable to build E14 scoring context.")

    placement = score_single_placement(
        context=ctx, em_mult=em_mult, wing_pts=wing_pts,
        weights_override=weights,
    )
    return {
        "underlying":     underlying,
        "entry_date":     ctx.entry_date,
        "expiry_date":    ctx.expiry_date,
        "as_of_date":     ctx.as_of_date,
        "placement":      placement.to_dict(),
        "context_source": source,
        "weights_used":   weights.as_dict(),
    }


# ---------------------------------------------------------------------------
# v2 E14-native advisor endpoint
# ---------------------------------------------------------------------------


@router.post("/api/ic-scenario/advisor")
def ic_scenario_advisor(body: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """v2 E14-native advisor.

    Request body forms:

    1. ``{"scenario": {...}}`` — pass a pre-computed scenario payload
       (typical Command Deck flow: the frontend already has the body
       from a prior ``/api/ic-scenario`` call).
    2. ``{"request": {...}}`` — re-run the scenario first, then
       narrate. Useful for direct API consumers that don't want to
       make two round-trips.

    When ``E14_ADVISOR_ENABLED=0`` or the LLM call fails, returns a
    deterministic fallback shell so the frontend UI never has to
    special-case "no advisor".
    """
    _ensure_enabled()
    f = get_flags()
    if not bool(getattr(f, "E14_ADVISOR_ENABLED", False)):
        raise HTTPException(status_code=404, detail="Engine 14 advisor disabled (E14_ADVISOR_ENABLED=0).")

    from backend.e14_advisor import generate_scenario_advisor

    scenario = body.get("scenario")
    if not scenario:
        req_body = body.get("request")
        if req_body:
            # Inline-compute the scenario. Delegate to the same parser +
            # run_scenario path the main endpoint uses so behavior is
            # identical (cache key included).
            req = _parse_request(req_body)
            key = _cache_key(req)
            with _scenario_cache_lock:
                cached = _scenario_cache.get(key)
            if cached is not None:
                scenario = cached
            else:
                try:
                    client = get_client()
                except Exception as e:
                    raise HTTPException(status_code=503, detail=f"ORATS client unavailable: {e}") from e
                try:
                    bz = get_benzinga_client_optional()
                except Exception:
                    bz = None
                try:
                    store = get_store_optional()
                except Exception:
                    store = None
                try:
                    scenario = run_scenario(req, client=client, benzinga_client=bz, store=store)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
                except Exception as e:
                    LOG.exception("engine14: run_scenario failed (advisor inline)")
                    raise HTTPException(
                        status_code=500,
                        detail=f"Scenario replay failed: {type(e).__name__}: {e}",
                    ) from e
                scenario = _augment_scenario_v2(scenario, body=req_body, flags=f)
                with _scenario_cache_lock:
                    _scenario_cache[key] = scenario

    if not scenario:
        raise HTTPException(
            status_code=400,
            detail="Advisor requires either {\"scenario\": {...}} or {\"request\": {...}} in the body.",
        )

    advisor = generate_scenario_advisor(scenario_payload=scenario, flags=f)
    return {
        "advisor":      advisor,
        "scenarioEcho": {
            "analoguesUsed":   scenario.get("analoguesUsed"),
            "entryState":      scenario.get("entryState"),
            "regime":          scenario.get("regime"),
            "outcomeDistribution": scenario.get("outcomeDistribution"),
        },
        "generatedAt":  dt.datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# Engine-14 ↔ Engine-2 reconciliation endpoint (Stage 1 + 1.5)
# ---------------------------------------------------------------------------

_reconcile_cache_lock = threading.Lock()
_reconcile_cache: TTLCache = TTLCache(maxsize=256, ttl=5 * 60)
_reconcile_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="engine14-reconcile",
)


def _compute_engine2_payload(under: str) -> Dict[str, Any]:
    """Run a vanilla Engine-2 scan with default params for reconciliation."""
    try:
        return compute_engine2_spx_ic(
            client=get_client(),
            benzinga_client=get_benzinga_client_optional(),
            flags=get_flags(),
            underlying_preference=under,
            entry_day="mon",
            years=3,
            widths=[0.8, 1.0, 1.2, 1.5, 2.0],
            risk_target_breach_pct=25.0,
            seasonality_mode="none",
        )
    except Exception as e:
        LOG.warning("reconcile: Engine-2 scan failed: %s", e)
        return {}


def _compute_advisor_with_timeout(
    engine2_payload: Dict[str, Any],
    timeout_s: float,
) -> Optional[Dict[str, Any]]:
    """Run the LLM advisor off-thread with a hard wall-clock timeout."""
    f = get_flags()
    if not getattr(f, "ENGINE2_ADVISOR_ENABLED", False):
        return None
    if not engine2_payload or not engine2_payload.get("current"):
        return None

    # Local import keeps the router import-light when advisor is disabled.
    from backend.engine2_advisor import generate_trade_analysis

    def _run() -> Dict[str, Any]:
        return generate_trade_analysis(
            engine2_payload=engine2_payload,
            width_analysis=engine2_payload.get("widthComparison"),
            flags=f,
        )

    fut = _reconcile_executor.submit(_run)
    try:
        return fut.result(timeout=float(timeout_s))
    except concurrent.futures.TimeoutError:
        LOG.info("reconcile: advisor timed out after %.1fs", timeout_s)
        fut.cancel()
        return None
    except Exception as e:
        LOG.warning("reconcile: advisor failed: %s", e)
        return None


@router.post("/api/ic-scenario/reconcile")
def ic_scenario_reconcile(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Cross-check a simulated scenario against Engine 2 + LLM advisor + live chain.

    Body can be either:

      * Previously-run output of ``/api/ic-scenario`` — pass under key
        ``scenario``. This avoids re-running the sim.
      * A raw scenario request — we run the simulation ourselves.

    Options:

      * ``runAdvisor`` (default True): kick off the LLM advisor (async,
        12s timeout). Set False for a fast deterministic-only reconcile.
      * ``checkLiveChain`` (default True): pull live NBBO for the four
        legs and include it as a credit anchor.
      * ``engine2`` (optional): pre-computed E2 payload. Saves ~2s.
    """
    _ensure_enabled()

    scenario = body.get("scenario")
    if not isinstance(scenario, dict) or not scenario.get("entryState"):
        # Treat the body itself as a scenario request.
        req = _parse_request(body.get("request") or body)
        try:
            client = get_client()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"ORATS client unavailable: {e}")
        try:
            bz = get_benzinga_client_optional()
        except Exception:
            bz = None
        try:
            store = get_store_optional()
        except Exception:
            store = None
        try:
            scenario = run_scenario(req, client=client, benzinga_client=bz, store=store)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            LOG.exception("reconcile: run_scenario failed")
            raise HTTPException(status_code=500, detail=f"Scenario replay failed: {type(e).__name__}: {e}")

    # Engine 2 scan — prefer caller-supplied payload; otherwise compute fresh.
    e2_payload = body.get("engine2")
    if not isinstance(e2_payload, dict) or not e2_payload:
        under = str((scenario.get("request") or {}).get("underlying") or "SPX").upper()
        e2_payload = _compute_engine2_payload(under)

    run_advisor = bool(body.get("runAdvisor", True))
    check_chain = bool(body.get("checkLiveChain", True))
    advisor_timeout_s = float(body.get("advisorTimeoutSeconds") or 12.0)

    advisor: Optional[Dict[str, Any]] = None
    live_chain: Optional[Dict[str, Any]] = None
    errors: Dict[str, str] = {}

    # Kick the advisor off FIRST so it runs concurrently with the live-chain fetch.
    advisor_fut = None
    if run_advisor and e2_payload:
        f = get_flags()
        if getattr(f, "ENGINE2_ADVISOR_ENABLED", False):
            advisor_fut = _reconcile_executor.submit(
                _compute_advisor_with_timeout, e2_payload, advisor_timeout_s,
            )

    if check_chain:
        try:
            req_fields = scenario.get("request") or {}
            client = get_client()
            live_chain = fetch_live_chain_nbbo(
                client,
                ticker=str(req_fields.get("underlying") or "SPX").upper(),
                expiry=str(req_fields.get("expiry") or ""),
                short_put=float(req_fields.get("short_put")),
                long_put=float(req_fields.get("long_put")),
                short_call=float(req_fields.get("short_call")),
                long_call=float(req_fields.get("long_call")),
            )
        except Exception as e:
            LOG.warning("reconcile: live chain fetch failed: %s", e)
            errors["liveChain"] = f"{type(e).__name__}: {e}"

    if advisor_fut is not None:
        try:
            advisor = advisor_fut.result(timeout=float(advisor_timeout_s) + 2.0)
        except concurrent.futures.TimeoutError:
            errors["advisor"] = f"timeout after {advisor_timeout_s}s"
        except Exception as e:
            errors["advisor"] = f"{type(e).__name__}: {e}"

    reconcile_payload = reconciliation.reconcile_full(
        scenario_result=scenario,
        engine2_payload=e2_payload or {},
        engine2_advisor=advisor,
        live_chain=live_chain,
    )

    return {
        "reconcile": reconcile_payload,
        "scenario": scenario,
        "engine2": {
            "asOfDate": (e2_payload or {}).get("asOfDate"),
            "current": (e2_payload or {}).get("current"),
            "expectedMove": (e2_payload or {}).get("expectedMove"),
            "strikeTargets": (e2_payload or {}).get("strikeTargets"),
            "deskConsensus": (e2_payload or {}).get("deskConsensus"),
            "recommendation": (e2_payload or {}).get("recommendation"),
            "widthComparison": (e2_payload or {}).get("widthComparison"),
            "oddsLikeNow": (e2_payload or {}).get("oddsLikeNow"),
        },
        "advisor": advisor,
        "liveChain": live_chain,
        "errors": errors or None,
        "generatedAt": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# Pre-submit guardrails (Stage 3)
# ---------------------------------------------------------------------------

def _pre_check_block(kind: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {"severity": "block", "kind": kind, "message": message, **extra}


def _pre_check_warn(kind: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {"severity": "warn", "kind": kind, "message": message, **extra}


@router.post("/api/ic-scenario/pre-check")
def ic_scenario_pre_check(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Fast pre-submit guardrails for the scenario form.

    Responsibilities:

      * Hard-block when any of the four strikes does not exist on the
        live option chain for the requested expiry. The response includes
        a ``suggestion`` with nearest-available strikes so the UI can
        offer a one-click fix.
      * Warn when the user-typed credit is outside the live NBBO or far
        off the width-comparison proxy / advisor estimate.
      * Warn when the user's EM multiple is below Engine 2's
        ``deskConsensus.suggestedEmFloor``.
      * Warn when the chosen cell violates the Engine 2 policy
        thresholds (breach / outside / MAE).

    Intentionally avoids the LLM advisor (sync path, sub-second).
    """
    _ensure_enabled()

    def _req_float(k: str) -> float:
        v = body.get(k)
        if v is None or v == "":
            raise HTTPException(status_code=400, detail=f"Missing required field: {k}")
        try:
            return float(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Field {k} must be numeric.")

    underlying = str(body.get("underlying") or "SPX").upper()
    if underlying != "SPX":
        raise HTTPException(status_code=400, detail="Engine 14 supports SPX only.")
    expiry = str(body.get("expiry") or "").strip()
    if not expiry:
        raise HTTPException(status_code=400, detail="expiry is required.")

    short_put = _req_float("shortPut")
    long_put = _req_float("longPut")
    short_call = _req_float("shortCall")
    long_call = _req_float("longCall")
    credit = _req_float("creditReceived")

    if not (long_put < short_put < short_call < long_call):
        raise HTTPException(
            status_code=400,
            detail="Strikes must satisfy: longPut < shortPut < shortCall < longCall.",
        )

    blocks: list[Dict[str, Any]] = []
    warnings: list[Dict[str, Any]] = []

    # --- Strike existence on the live chain -------------------------------
    try:
        client = get_client()
    except Exception as e:
        return {
            "ok": True,
            "blocks": [],
            "warnings": [_pre_check_warn(
                "liveChainUnavailable",
                f"Live chain unavailable ({type(e).__name__}); proceeding without strike verification.",
            )],
            "liveChain": None,
            "suggestion": None,
        }

    strike_check = validate_strikes_exist(
        client, ticker=underlying, expiry=expiry,
        short_put=short_put, long_put=long_put,
        short_call=short_call, long_call=long_call,
    )

    suggestion: Optional[Dict[str, Any]] = None
    if strike_check.get("expiryFound") and not strike_check.get("ok"):
        missing = strike_check.get("missing") or []
        blocks.append(_pre_check_block(
            "missingStrike",
            f"{len(missing)} leg(s) do not exist for {underlying} {expiry}.",
            missing=missing,
        ))
        # Build a full suggestion struct with nearest-strike replacements.
        fix = {
            "shortPut": short_put, "longPut": long_put,
            "shortCall": short_call, "longCall": long_call,
        }
        for m in missing:
            fix[m["leg"]] = m["nearest"]
        suggestion = {"strikes": fix}

    if not strike_check.get("expiryFound"):
        warnings.append(_pre_check_warn(
            "liveChainUnavailable",
            f"No live chain data for {underlying} {expiry}. Strike existence not verified.",
        ))

    # --- Live NBBO credit anchor ------------------------------------------
    live_chain: Optional[Dict[str, Any]] = None
    if strike_check.get("ok"):
        try:
            live_chain = fetch_live_chain_nbbo(
                client, ticker=underlying, expiry=expiry,
                short_put=short_put, long_put=long_put,
                short_call=short_call, long_call=long_call,
            )
        except Exception as e:
            LOG.warning("pre-check: live NBBO fetch failed: %s", e)

    if live_chain:
        mid = float(live_chain.get("mid") or 0.0)
        net_bid = live_chain.get("netBid")
        net_ask = live_chain.get("netAsk")
        inside = True
        if net_bid is not None and credit < float(net_bid) - 1e-6:
            inside = False
        if net_ask is not None and credit > float(net_ask) + 1e-6:
            inside = False
        if not inside:
            warnings.append(_pre_check_warn(
                "creditOutsideNBBO",
                f"User credit ${credit:.2f} is outside live NBBO "
                f"[${net_bid:.2f}, ${net_ask:.2f}] for mid ${mid:.2f}.",
                userCredit=credit, nbbo={"bid": net_bid, "ask": net_ask, "mid": mid},
            ))
        elif mid > 0 and abs(credit - mid) / mid > 0.25:
            warnings.append(_pre_check_warn(
                "creditFarFromMid",
                f"User credit ${credit:.2f} is >25% off live mid ${mid:.2f}.",
                userCredit=credit, mid=mid,
            ))

    # --- Engine 2 policy / floor / box ------------------------------------
    try:
        e2 = _compute_engine2_payload(underlying)
    except Exception:
        e2 = {}

    if e2:
        # Synthesize a minimal "scenario-like" dict so we can reuse the
        # single-check helpers from reconciliation.
        em = e2.get("expectedMove") or {}
        spot = float(em.get("smartSpotPrice") or em.get("spotPrice") or 0.0) or None
        put_dist = abs(spot - short_put) if spot else 0.0
        call_dist = abs(short_call - spot) if spot else 0.0
        em_pct = float(em.get("oratsExpectedMovePct") or em.get("delayedImpliedMovePct") or 0.0) or None
        em_dollars = (em_pct / 100.0) * spot if (em_pct and spot) else None
        user_em_mult = None
        if em_dollars and em_dollars > 0:
            user_em_mult = round(((put_dist + call_dist) / 2.0) / em_dollars, 2)
        wing_width = float(min(short_put - long_put, long_call - short_call))

        synthetic_scenario = {
            "request": {
                "short_put": short_put, "long_put": long_put,
                "short_call": short_call, "long_call": long_call,
                "credit_received": credit, "underlying": underlying,
                "expiry": expiry,
            },
            "entryState": {
                "userSpot": spot,
                "userEmPct": em_pct,
                "userEmMultiple": user_em_mult,
                "wingWidth": wing_width,
                "regimeBucket": ((e2.get("current") or {}).get("regime") or {}).get("bucket"),
                "regimeSource": "em_proxy",
            },
        }

        policy_chip = reconciliation._check_policy(synthetic_scenario, e2)
        floor_chip = reconciliation._check_desk_floor(synthetic_scenario, e2)
        box_chip = reconciliation._check_em_multiple_label(synthetic_scenario, e2)

        if policy_chip["status"] == "mismatch":
            warnings.append(_pre_check_warn(
                "policyMultipleViolations",
                policy_chip["note"],
                chip=policy_chip,
            ))
        elif policy_chip["status"] == "drift":
            warnings.append(_pre_check_warn(
                "policyDrift",
                policy_chip["note"],
                chip=policy_chip,
            ))

        if floor_chip["status"] == "mismatch":
            warnings.append(_pre_check_warn(
                "belowDeskEmFloor",
                floor_chip["note"],
                chip=floor_chip,
            ))

        if box_chip["status"] in ("drift", "mismatch"):
            warnings.append(_pre_check_warn(
                "emMultipleMisaligned",
                box_chip["note"],
                chip=box_chip,
            ))

    return {
        "ok": len(blocks) == 0,
        "blocks": blocks,
        "warnings": warnings,
        "liveChain": live_chain,
        "availableStrikes": strike_check.get("availableStrikes") or [],
        "suggestion": suggestion,
    }


@router.get("/api/ic-scenario/health")
def ic_scenario_health() -> Dict[str, Any]:
    """Cache coverage + enablement probe, used by the UI before enabling the Run button."""
    f = get_flags()
    enabled = bool(getattr(f, "ENABLE_ENGINE14_IC_SCENARIO", False))
    try:
        cov = chain_cache.cache_coverage(ticker="SPX")
    except Exception as e:
        cov = {"ticker": "SPX", "daysCovered": 0, "error": f"{type(e).__name__}: {e}"}
    return {
        "enabled": enabled,
        "chainCache": cov,
        "minAnalogues": int(f.ENGINE14_MIN_ANALOGUES),
        "lookbackYears": int(f.ENGINE14_LOOKBACK_YEARS),
    }


@router.get("/api/ic-scenario/coverage")
def ic_scenario_coverage() -> Dict[str, Any]:
    _ensure_enabled()
    return {"SPX": chain_cache.cache_coverage(ticker="SPX")}


# NOTE: The per-card LLM explainer used to live here as /api/ic-scenario/
# explain-card + /api/ic-scenario/explain-card/catalog. Those routes are
# now served by the shared Raven Desk Insight v2 router
# (backend/routers/desk_insight.py), which routes them through the unified
# nine-section pipeline while keeping the legacy URL contract intact.


# ---------------------------------------------------------------------------
# Phase 3: trade-journal hand-off
# ---------------------------------------------------------------------------

@router.post("/api/ic-scenario/journal")
def ic_scenario_journal(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Persist a simulated IC to the Engine 2 trade journal.

    Expected body:
      { "scenario":  <full payload from /api/ic-scenario>,
        "request":   <original form submission>,
        "reconcile": <optional: full /reconcile payload. If omitted, we
                     compute a fresh one here so the trade record always
                     captures what the desk saw at entry>,
        "engine2":   <optional: already-fetched Engine 2 scan to avoid
                     re-running it during snapshot capture>,
        "note":      "optional free-text" }
    """
    _ensure_enabled()
    scenario = body.get("scenario") or {}
    form = body.get("request") or scenario.get("request") or {}
    if not form:
        raise HTTPException(status_code=400, detail="request payload missing.")

    # --- Capture a reconcile snapshot at entry -------------------------------
    # Callers that just ran /reconcile can pass the payload through; otherwise
    # we synthesize a deterministic-only snapshot (cheap, no LLM / live NBBO)
    # so every logged trade carries a "what the desk knew at entry" chip.
    reconcile_full_payload: Optional[Dict[str, Any]] = None
    raw_reconcile = body.get("reconcile")
    if isinstance(raw_reconcile, dict) and raw_reconcile.get("overall"):
        reconcile_full_payload = raw_reconcile
    elif scenario:
        e2_payload = body.get("engine2")
        if not isinstance(e2_payload, dict) or not e2_payload:
            under = str(form.get("underlying") or "SPX").upper()
            e2_payload = _compute_engine2_payload(under) or {}
        try:
            reconcile_full_payload = reconciliation.reconcile_deterministic(
                scenario_result=scenario,
                engine2_payload=e2_payload,
            )
        except Exception:
            LOG.exception("journal: reconcile_deterministic failed; logging trade without snapshot")
            reconcile_full_payload = None

    reconcile_snapshot = reconciliation.summarize_for_journal(reconcile_full_payload)

    # Normalize into the Engine 2 trade-log schema.
    strikes = {
        "shortPut": form.get("short_put") or form.get("shortPut"),
        "longPut": form.get("long_put") or form.get("longPut"),
        "shortCall": form.get("short_call") or form.get("shortCall"),
        "longCall": form.get("long_call") or form.get("longCall"),
    }
    entry_context: Dict[str, Any] = {
        "engine14Scenario": scenario,
        "note": str(body.get("note") or "").strip() or None,
    }
    if reconcile_snapshot:
        entry_context["reconcile"] = reconcile_snapshot

    trade_data = {
        "source": "engine14",
        "entry": {
            "underlying": str(form.get("underlying") or "SPX").upper(),
            "entryDate": form.get("entry_date") or form.get("entryDate"),
            "expiry": form.get("expiry"),
            "strikes": strikes,
            "creditReceived": form.get("credit_received") or form.get("creditReceived"),
            "profitTargetPct": form.get("profit_target_pct") or form.get("profitTargetPct"),
            "stopLossPct": form.get("stop_loss_pct") or form.get("stopLossPct"),
        },
        "entryContext": entry_context,
        "advisorVerdict": {
            "engine": 14,
            "expectedValue": scenario.get("expectedValue"),
            "outcomeDistribution": scenario.get("outcomeDistribution"),
            "adjustedOutcomeDistribution": scenario.get("adjustedOutcomeDistribution"),
            "exitRules": scenario.get("exitRulesOptimization"),
        },
    }

    trade_id = log_trade(trade_data)
    if trade_id is None:
        raise HTTPException(
            status_code=503,
            detail="Trade journal unavailable (Redis not configured).",
        )
    return {
        "tradeId": trade_id,
        "viewUrl": f"/spx?tradeId={trade_id}",
        "reconcile": reconcile_snapshot,
    }


@router.get("/api/ic-scenario/review")
def ic_scenario_review(trade_id: str = Query(..., alias="tradeId")) -> Dict[str, Any]:
    """Post-trade review: compare the stored simulation to the closed outcome."""
    _ensure_enabled()
    trade = get_trade(trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found.")

    scenario = ((trade.get("entryContext") or {}).get("engine14Scenario")) or {}
    if not scenario:
        raise HTTPException(
            status_code=400,
            detail="This trade has no Engine 14 scenario attached — nothing to review.",
        )

    base = scenario.get("outcomeDistribution") or {}
    adjusted = scenario.get("adjustedOutcomeDistribution") or {}
    predicted = {
        "meanPnlPct": (scenario.get("expectedValue") or {}).get("meanPnlPct"),
        "medianPnlPct": (scenario.get("expectedValue") or {}).get("medianPnlPct"),
        "fullCollectPct": (base.get("fullCollect") or {}).get("pct"),
        "earlyTargetPct": (base.get("earlyTarget") or {}).get("pct"),
        "breachPct": (base.get("breach") or {}).get("pct"),
        "stopOutPct": (base.get("stopOut") or {}).get("pct"),
    }
    predicted_adj = {
        "fullCollectPct": (adjusted.get("fullCollect") or {}).get("pct"),
        "earlyTargetPct": (adjusted.get("earlyTarget") or {}).get("pct"),
        "breachPct": (adjusted.get("breach") or {}).get("pct"),
        "stopOutPct": (adjusted.get("stopOut") or {}).get("pct"),
    } if adjusted else None

    status = str(trade.get("status") or "active")
    outcome = trade.get("outcome") or {}
    close_reason = trade.get("closeReason")
    actual: Dict[str, Any] = {
        "status": status,
        "closedAt": trade.get("closedAt"),
        "closeReason": close_reason,
    }
    if outcome:
        actual["pnlPct"] = outcome.get("pnlPct")
        actual["pnlDollars"] = outcome.get("pnlDollars")
        actual["daysHeld"] = outcome.get("daysHeld")

    # Verdict: was the sim roughly right?
    verdict: Optional[str] = None
    if status in ("closed",) and actual.get("pnlPct") is not None:
        actual_pnl = float(actual["pnlPct"])
        pred_mean = predicted.get("meanPnlPct")
        if pred_mean is not None:
            diff = actual_pnl - float(pred_mean)
            if abs(diff) <= 15.0:
                verdict = f"Sim within ±15pp of actual (Δ={diff:+.1f}pp)."
            elif diff > 0:
                verdict = f"Actual beat sim by {diff:.1f}pp — tailwinds stronger than modeled."
            else:
                verdict = f"Actual underperformed sim by {-diff:.1f}pp — headwinds stronger than modeled."

    entry_reconcile = (trade.get("entryContext") or {}).get("reconcile")

    # v2: surface the MI v2 regime-at-entry when the stored scenario
    # carries it (post-refactor trades) so the verdict language matches
    # the scan's. Pre-refactor trades keep the legacy DMS label via
    # entryState.regimeBucket / regimeSource. New trades get MI v2 as
    # the source of truth.
    entry_state = scenario.get("entryState") or {}
    regime_mi_v2 = None
    regime_src = entry_state.get("regimeSource")
    regime_at_entry = {
        "bucket": entry_state.get("regimeBucket"),
        "source": regime_src,
        "mi_v2":  None,
    }
    scenario_regime = scenario.get("regime") if isinstance(scenario.get("regime"), dict) else None
    if scenario_regime:
        regime_mi_v2 = scenario_regime.get("mi_v2")
    if regime_mi_v2 is None:
        regime_mi_v2 = entry_state.get("regimeMiV2")
    if isinstance(regime_mi_v2, dict):
        regime_at_entry["mi_v2"] = regime_mi_v2
        # Prefer MI v2 label as the canonical regime-at-entry when
        # available (post-v2 scenarios). Pre-v2 trades keep regimeBucket.
        if regime_mi_v2.get("label"):
            regime_at_entry["label"] = regime_mi_v2.get("label")
            regime_at_entry["source"] = regime_mi_v2.get("source") or "mi_v2"

    return {
        "tradeId": trade_id,
        "predicted": predicted,
        "predictedAdjusted": predicted_adj,
        "actual": actual,
        "verdict": verdict,
        "scenarioVersion": scenario.get("version"),
        "analoguesUsed": scenario.get("analoguesUsed"),
        "entryReconcile": entry_reconcile,
        "regimeAtEntry":  regime_at_entry,
    }


# ---------------------------------------------------------------------------
# Admin: backfill endpoint
# ---------------------------------------------------------------------------

_backfill_state: Dict[str, Any] = {"running": False, "started_at": None, "progress": None, "error": None}
_backfill_lock = threading.Lock()


def _check_admin_token(x_admin_token: Optional[str]) -> None:
    f = get_flags()
    expected = str(getattr(f, "ENGINE14_ADMIN_TOKEN", "") or os.getenv("ENGINE14_ADMIN_TOKEN", "")).strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="ENGINE14_ADMIN_TOKEN not configured on server.",
        )
    if not x_admin_token or str(x_admin_token).strip() != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Token.")


def _run_backfill_bg(*, years: float, max_dte: int, resume: bool, delay_ms: int) -> None:
    """Background worker: mirrors scripts/engine14_backfill_chains.py."""
    global _backfill_state
    import time
    from backend.orats_client import OratsClient, OratsError
    from backend.spx_ic.ohlc import fetch_dailies_ohlc_range
    try:
        today = dt.date.today()
        start = today - dt.timedelta(days=int(float(years) * 370))
        client = OratsClient.from_env()
        bars = fetch_dailies_ohlc_range(client, ticker="SPX", start=start, end=today)
        dates = [b.trade_date for b in bars if b.close is not None]
        if resume:
            cached = set(chain_cache.fetch_cached_trade_dates(ticker="SPX"))
            dates = [d for d in dates if d not in cached]
        total = len(dates)
        _backfill_state["progress"] = {"total": total, "completed": 0, "failed": 0}
        delay = max(0.0, float(delay_ms) / 1000.0)
        for i, td in enumerate(dates, start=1):
            try:
                chain_cache.fetch_and_cache_day(
                    client, ticker="SPX", trade_date=td, max_dte=int(max_dte),
                )
                _backfill_state["progress"]["completed"] = i
            except OratsError as e:
                LOG.warning("backfill: ORATS error at %s: %s", td, e)
                _backfill_state["progress"]["failed"] = (_backfill_state["progress"]["failed"] or 0) + 1
            except Exception as e:
                LOG.exception("backfill: unexpected error at %s: %s", td, e)
                _backfill_state["progress"]["failed"] = (_backfill_state["progress"]["failed"] or 0) + 1
            if delay:
                time.sleep(delay)
    except Exception as e:
        LOG.exception("backfill: fatal error")
        _backfill_state["error"] = f"{type(e).__name__}: {e}"
    finally:
        _backfill_state["running"] = False
        _backfill_state["finished_at"] = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z"


@router.post("/api/ic-scenario/backfill")
def ic_scenario_backfill(
    body: Dict[str, Any] = Body(default_factory=dict),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
) -> Dict[str, Any]:
    """Kick off a background chain-cache backfill. Token-gated.

    Body: {"years": 2.0, "maxDte": 45, "resume": true, "delayMs": 250}
    Poll `/api/ic-scenario/backfill/status` for progress.
    """
    _ensure_enabled()
    _check_admin_token(x_admin_token)

    f = get_flags()
    years = float(body.get("years") or f.ENGINE14_LOOKBACK_YEARS)
    years = max(0.1, min(float(f.ENGINE14_BACKFILL_MAX_YEARS), years))
    max_dte = int(body.get("maxDte") or 45)
    resume = bool(body.get("resume", True))
    delay_ms = int(body.get("delayMs") or 250)

    with _backfill_lock:
        if _backfill_state.get("running"):
            raise HTTPException(status_code=409, detail="Backfill already in progress.")
        _backfill_state.update({
            "running": True,
            "started_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
            "finished_at": None,
            "progress": None,
            "error": None,
            "params": {"years": years, "maxDte": max_dte, "resume": resume, "delayMs": delay_ms},
        })
        t = threading.Thread(
            target=_run_backfill_bg,
            kwargs={"years": years, "max_dte": max_dte, "resume": resume, "delay_ms": delay_ms},
            daemon=True,
            name="engine14-backfill",
        )
        t.start()
    return {"started": True, "params": _backfill_state["params"]}


@router.get("/api/ic-scenario/backfill/status")
def ic_scenario_backfill_status() -> Dict[str, Any]:
    """Open status endpoint (no token) — progress only, no destructive ops."""
    _ensure_enabled()
    cov = chain_cache.cache_coverage(ticker="SPX")
    return {
        "running": bool(_backfill_state.get("running")),
        "startedAt": _backfill_state.get("started_at"),
        "finishedAt": _backfill_state.get("finished_at"),
        "progress": _backfill_state.get("progress"),
        "error": _backfill_state.get("error"),
        "params": _backfill_state.get("params"),
        "coverage": cov,
    }


# ---------------------------------------------------------------------------
# Phase B: modifier-coefficients inspection
# ---------------------------------------------------------------------------

def _summarize_coeff_sources(coeffs: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    """Count hand_coded vs empirical buckets per modifier section."""
    out: Dict[str, Dict[str, int]] = {}
    cal_kws = ((coeffs.get("calendar") or {}).get("keywords")) or []
    out["calendar"] = {
        "empirical": sum(1 for r in cal_kws if r.get("source") == "empirical"),
        "handCoded": sum(1 for r in cal_kws if r.get("source") != "empirical"),
    }
    for section in ("dealerGamma", "creditStress", "gapRegime"):
        rows = (coeffs.get(section) or {})
        emp = sum(1 for v in rows.values() if isinstance(v, dict) and v.get("source") == "empirical")
        hc = sum(1 for v in rows.values() if isinstance(v, dict) and v.get("source") != "empirical")
        out[section] = {"empirical": int(emp), "handCoded": int(hc)}
    return out


@router.get("/api/ic-scenario/regime-features/coverage")
def ic_scenario_regime_features_coverage() -> Dict[str, Any]:
    """Open coverage probe for the Phase C1 multi-factor regime features store."""
    _ensure_enabled()
    try:
        cov = regime_features.coverage()
    except Exception as e:
        LOG.exception("regime features coverage failed")
        cov = {"error": f"{type(e).__name__}: {e}"}
    return {"store": cov}


@router.get("/api/ic-scenario/modifier-coefficients")
def ic_scenario_modifier_coefficients(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    reload: bool = Query(default=False, description="Force re-read from disk."),
) -> Dict[str, Any]:
    """Inspect the currently-loaded Phase B modifier coefficients.

    Token-gated because the learned values are considered tuning data.
    The returned payload includes the raw coefficients, the resolved
    source path, and a per-section hand-coded vs empirical tally.
    """
    _ensure_enabled()
    _check_admin_token(x_admin_token)
    f = get_flags()
    path = str(getattr(f, "ENGINE14_MODIFIER_COEFFICIENTS_PATH", "") or "")
    coeffs = load_modifier_coefficients(force_reload=bool(reload))
    return {
        "path": path,
        "exists": bool(path and os.path.exists(path)),
        "coefficients": coeffs,
        "sources": _summarize_coeff_sources(coeffs),
    }
