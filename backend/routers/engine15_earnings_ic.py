"""Engine 15 — Earnings IC Scenario Simulator routes.

Mirrors the shape and caching pattern of
``backend/routers/engine14_ic_scenario.py`` but specialized for
single-name earnings IC plays driven by Engine 1's output.

Public endpoints:

  * ``POST /api/earnings-ic/scan``            — proxy to Engine 1 (breach stats + enrichment)
  * ``POST /api/earnings-ic/scenario``        — main replay engine
  * ``POST /api/earnings-ic/pre-check``       — strike existence + NBBO sanity
  * ``POST /api/earnings-ic/reconcile``       — cross-check vs live chain + E1 advisor
  * ``POST /api/earnings-ic/journal``         — log trade into existing E1 Redis store
  * ``GET  /api/earnings-ic/review``          — compare stored sim against closed outcome
  * ``GET  /api/earnings-ic/coverage``        — per-ticker chain cache coverage
  * ``GET  /api/earnings-ic/health``          — flag + cache probe
  * ``POST /api/earnings-ic/backfill``        — admin-only per-ticker backfill (async)
  * ``GET  /api/earnings-ic/backfill/status`` — progress probe
  * ``GET  /api/earnings-ic/explain-card/catalog`` / ``POST /api/earnings-ic/explain-card``
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from cachetools import TTLCache
from fastapi import APIRouter, Body, Header, HTTPException, Query

from backend.config import get_flags
from backend.deps import (
    LOG as _APP_LOG,
    get_benzinga_client_optional,
    get_client,
    get_client_optional,
)
from backend.engine14 import chain_cache
from backend.redis_store import get_store_optional

LOG = logging.getLogger("engine15.router")

router = APIRouter()

_SCENARIO_CACHE_LOCK = threading.Lock()
_SCENARIO_CACHE: TTLCache = TTLCache(maxsize=256, ttl=10 * 60)

_BACKFILL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="engine15-backfill",
)

# Per-ticker backfill progress store. Keyed by ticker UPPER.
_BACKFILL_STATE: Dict[str, Dict[str, Any]] = {}
_BACKFILL_LOCK = threading.Lock()


def _ensure_enabled() -> None:
    f = get_flags()
    if not getattr(f, "ENABLE_ENGINE15_EARNINGS_IC", False):
        raise HTTPException(
            status_code=404,
            detail="Engine 15 disabled (ENABLE_ENGINE15_EARNINGS_IC=0).",
        )


def _check_admin_token(x_admin_token: Optional[str]) -> None:
    f = get_flags()
    expected = str(
        getattr(f, "ENGINE15_ADMIN_TOKEN", "") or os.getenv("ENGINE15_ADMIN_TOKEN", "")
    ).strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="ENGINE15_ADMIN_TOKEN not configured on server.",
        )
    if not x_admin_token or str(x_admin_token).strip() != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Token.")


def _norm_ticker(body: Dict[str, Any]) -> str:
    t = str(body.get("ticker") or "").strip().upper()
    if not t:
        raise HTTPException(status_code=400, detail="ticker is required.")
    return t


def _discover_next_event(
    client, *, ticker: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Resolve the forward earnings event for Engine 15.

    Resolution ladder:

    1. Trust ORATS ``/cores`` ``nextErn`` when it publishes a plausible date
       (parses, within +180d / -10d).
    2. Fall back to the **upcoming Friday expiry** already computed by Engine 1
       under ``payload.expectedMove`` — the same chain E1 uses for its
       straddle-EM / strike-target math. The desk only routes single-name
       earnings trades to this engine when the ticker reports that week, so
       "that week's Friday expiry" is always the correct reference chain.
       We derive the likely earnings date via the quarterly cadence of the
       most recent prior event (``events[0].earnDate`` + ~91 days).

    Returns ``None`` only if *both* ladders produce nothing — callers then
    leave ``nextEvent`` empty and the UI falls back to manual entry.
    """
    import datetime as _dt
    from backend.earnings_logic import classify_timing

    current = payload.get("current") or {}
    expected_move = payload.get("expectedMove") or {}
    events = payload.get("events") or []

    # --- 1. ORATS nextErn (primary) -------------------------------------
    next_date_orats: Optional[str] = None
    raw_tod_orats: Any = None
    days_orats: Any = None
    try:
        snap = client.cores(
            ticker=ticker,
            fields="ticker,tradeDate,stockPrice,impErnMv,nextErn,nextErnTod,daysToNextErn",
        )
        rows = getattr(snap, "rows", None) or []
        if rows:
            row = rows[0] or {}
            cand = str(row.get("nextErn") or "").strip()[:10]
            if cand and cand not in ("0000-00-00", "1970-01-01"):
                try:
                    parsed = _dt.date.fromisoformat(cand)
                    today = _dt.date.today()
                    if (today - parsed).days <= 10 and (parsed - today).days <= 180:
                        next_date_orats = cand
                        raw_tod_orats = row.get("nextErnTod")
                        days_orats = row.get("daysToNextErn")
                except ValueError:
                    pass
    except Exception as e:
        LOG.debug("cores snapshot failed for %s: %s", ticker, e)

    # --- 2. Friday-expiry fallback --------------------------------------
    # Engine 1's ``expectedMove.expiry`` is the near-dated Friday the
    # straddle-EM was computed against. When ORATS' earnings-date feed is
    # stale or missing, this is the chain the desk actually cares about.
    friday_expiry = str(expected_move.get("expiry") or "").strip()[:10]

    em_live = current.get("impliedMovePct")
    em_delayed = current.get("delayedImpliedMovePct")
    em_straddle_pct = expected_move.get("expectedMovePct")
    em_straddle_dollars = expected_move.get("expectedMoveDollars")

    if next_date_orats:
        timing = classify_timing(raw_tod_orats) if raw_tod_orats is not None else "UNK"
        em_pct = None
        if em_live is not None:
            em_pct = float(em_live)
            em_src = "orats_snapshot_live"
        elif em_delayed is not None:
            em_pct = float(em_delayed)
            em_src = "orats_snapshot_delayed"
        elif em_straddle_pct is not None:
            em_pct = float(em_straddle_pct)
            em_src = "straddle_em"
        else:
            em_src = "none"
        try:
            days = int(days_orats) if days_orats is not None else None
        except Exception:
            days = None
        return {
            "earnDateNext": next_date_orats,
            "timingPlanned": timing,
            "anncTod": None if raw_tod_orats is None else str(raw_tod_orats),
            "daysToNext": days,
            "impliedMovePctPlanned": em_pct,
            "impliedMoveSource": em_src,
            "pricingExpiry": friday_expiry or None,
            "source": "orats_snapshot",
            "confidence": "HIGH" if timing in ("BMO", "AMC") else "MED",
            "notes": [],
        }

    # ORATS didn't give us a usable earnings date. Use Engine 1's
    # ``expectedMove.expiry`` as the pricing reference and infer the likely
    # earnings date from the most recent quarter's anchor.
    if not friday_expiry:
        return None

    # Cadence from events[0] — most recent prior earnings + ~91 days.
    inferred_date: Optional[str] = None
    timing_inferred = "UNK"
    if events:
        try:
            last = events[0] or {}
            anchor = str(last.get("earnDate") or "").strip()[:10]
            if anchor:
                d = _dt.date.fromisoformat(anchor)
                # Walk forward in 91-day steps until we land within the next
                # 10 days-earlier-to-45-days-later of today (covers early or
                # late quarterly reporters).
                today = _dt.date.today()
                for step in range(1, 9):
                    guess = d + _dt.timedelta(days=91 * step)
                    if (guess - today).days <= 45 and (today - guess).days <= 10:
                        inferred_date = guess.isoformat()
                        break
            tl = str(last.get("timing") or "").strip().upper()
            if tl in ("BMO", "AMC"):
                timing_inferred = tl
            else:
                timing_inferred = classify_timing(last.get("anncTod")) or "UNK"
        except Exception:
            pass

    # If cadence inference produced nothing sensible, bias the earnings
    # date to the business day before the Friday expiry (Thursday-PM /
    # Friday-AM coverage), which is where most earnings weeks land.
    if not inferred_date:
        try:
            fri = _dt.date.fromisoformat(friday_expiry)
            # Earnings typically land Mon-Thu AM or Wed-Thu PM of the same
            # week; use Wednesday of the expiry's week as a placeholder so
            # the UI prefills something reasonable the desk can adjust.
            # (Friday - 2 business days = Wednesday.)
            inferred_date = (fri - _dt.timedelta(days=2)).isoformat()
        except ValueError:
            pass

    em_pct: Optional[float] = None
    em_src = "none"
    if em_delayed is not None:
        em_pct = float(em_delayed)
        em_src = "orats_delayed"
    elif em_straddle_pct is not None:
        em_pct = float(em_straddle_pct)
        em_src = "straddle_em"
    elif em_live is not None:
        em_pct = float(em_live)
        em_src = "orats_live"

    return {
        "earnDateNext": inferred_date,
        "timingPlanned": timing_inferred,
        "anncTod": None,
        "daysToNext": None,
        "impliedMovePctPlanned": em_pct,
        "impliedMoveSource": em_src,
        "pricingExpiry": friday_expiry,
        "source": "friday_expiry_fallback",
        "confidence": "MED" if timing_inferred in ("BMO", "AMC") else "LOW",
        "notes": [
            "ORATS nextErn was missing or stale; Engine 15 derived the next "
            "event from the upcoming Friday expiry (Engine 1's expectedMove "
            "chain). Confirm the earnings date manually before trading."
        ],
    }


# ---------------------------------------------------------------------------
# /api/earnings-ic/scan — Engine 1 proxy
# ---------------------------------------------------------------------------

def _run_engine1(ticker: str, *, n: int = 20, years: int = 5, k: float = 1.0) -> Dict[str, Any]:
    """Run Engine 1's compute_breach_stats + VRP enrichment for a single ticker.

    This is a superset of what ``/api/breach`` returns: it adds VRP, width
    comparison, entry quality, desk consensus, and em preference, matching
    what ``routers/engine1_breach.py::breach_compare::fetch_single`` does.
    We inline it here (rather than HTTP-hopping to /api/breach) to avoid
    rehydrating the FastAPI request cycle.
    """
    from backend.e1_vrp_engine import (
        compute_e1_desk_consensus,
        compute_earnings_width_comparison,
        compute_em_preference,
        compute_entry_quality,
        compute_vrp_score,
    )
    from backend.earnings_logic import compute_breach_stats
    from backend.go_no_go import compute_go_no_go

    f = get_flags()
    client = get_client()
    benzinga_client = get_benzinga_client_optional()

    payload = compute_breach_stats(
        client=client,
        ticker=ticker,
        n=int(n),
        years=int(years),
        k=float(k),
        trade_builder_inputs=None,
        flags_override=f,
        benzinga_client=benzinga_client,
    )
    try:
        payload["goNoGo"] = compute_go_no_go(
            client, ticker=ticker, payload=payload, benzinga_client=benzinga_client,
        )
    except Exception as e:
        LOG.warning("scan goNoGo failed for %s: %s", ticker, e)

    events = payload.get("events") or []
    current = payload.get("current") or {}
    current_em_pct: Optional[float] = None
    try:
        current_em_pct = float(current.get("impliedMovePct") or 0) or None
    except Exception:
        pass
    # Pre-market on announcement day: ORATS' live /cores may publish
    # ``impErnMv=null``. Engine 1 stores the last-known delayed snapshot
    # under ``current.delayedImpliedMovePct``; fall back to it so the
    # scan's VRP/breach-at-current-EM stats stay populated for the UI.
    if current_em_pct is None:
        try:
            d = current.get("delayedImpliedMovePct")
            if d is not None:
                f_d = float(d)
                if f_d > 0:
                    current_em_pct = f_d
        except Exception:
            pass

    # Compute a minimal ``nextEvent`` for UI prefill without running the
    # full ENABLE_MONTE_CARLO_EARNINGS path. We pull ORATS /cores directly:
    # the same snapshot that Engine 1's MC branch consults. This keeps the
    # scan lightweight (one extra /cores call, no MC simulation latency).
    try:
        next_event = _discover_next_event(client, ticker=ticker, payload=payload)
        if next_event:
            # Honor Engine 1 if it already populated nextEvent (MC flag on).
            existing = payload.get("nextEvent") or {}
            if not existing.get("earnDateNext"):
                payload["nextEvent"] = next_event
    except Exception as e:
        LOG.debug("scan nextEvent discovery failed for %s: %s", ticker, e)

    try:
        vrp = compute_vrp_score(events, current_implied_move_pct=current_em_pct)
        payload["vrpAnalysis"] = vrp

        em_mults = [float(x.strip()) for x in str(f.E1_EM_MULTS).split(",") if x.strip()]
        wing_pts = [float(x.strip()) for x in str(f.E1_WING_WIDTH_PTS).split(",") if x.strip()]
        stock_price: Optional[float] = None
        try:
            stock_price = float(current.get("stockPrice") or 0) or None
        except Exception:
            pass
        wc, em_breach = compute_earnings_width_comparison(
            events,
            em_mults=em_mults, wing_pts=wing_pts,
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
        payload["deskConsensus"] = dc

        payload["emPreference"] = compute_em_preference(
            em_breach, vrp.get("vrpScore"), eq.get("entryQuality"),
        )
    except Exception as e:
        LOG.warning("scan VRP enrichment failed for %s: %s", ticker, e)

    return payload


@router.post("/api/earnings-ic/scan")
def earnings_ic_scan(body: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """Run Engine 1 on a single ticker. Mirrors /api/breach but returns the
    full VRP-enriched payload the Engine 15 UI needs in one RTT."""
    _ensure_enabled()
    ticker = _norm_ticker(body)
    n = int(body.get("n") or 20)
    years = int(body.get("years") or 5)
    k = float(body.get("k") or 1.0)

    try:
        payload = _run_engine1(ticker, n=n, years=years, k=k)
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("scan failed for %s", ticker)
        raise HTTPException(status_code=502, detail=f"Engine 1 scan failed: {type(e).__name__}: {e}")

    # Compute chain coverage so the UI can tell the user whether they can
    # run a scenario immediately or should kick off a backfill first.
    try:
        coverage = chain_cache.cache_coverage(ticker=ticker)
    except Exception:
        coverage = {"ticker": ticker, "daysCovered": 0}

    # Provide a flat Engine-1 summary alongside the raw payload so the UI
    # can render E1-parity cards (ORATS EM + straddle EM + strike targets)
    # without each frontend having to re-implement the dig-through logic.
    try:
        from backend.engine15.simulator import _summarize_engine1
        summary = _summarize_engine1(payload)
    except Exception as e:
        LOG.debug("engine1Summary build failed for %s: %s", ticker, e)
        summary = {}

    return {
        "ticker": ticker,
        "engine1": payload,
        "engine1Summary": summary,
        "chainCoverage": coverage,
        "generatedAt": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# /api/earnings-ic/scenario — main entrypoint
# ---------------------------------------------------------------------------

def _parse_scenario_body(body: Dict[str, Any]):
    """Validate + coerce the request body into an :class:`EarningsIcRequest`."""
    from backend.engine15.simulator import EarningsIcRequest

    def _req_float(k: str) -> float:
        v = body.get(k)
        if v is None or v == "":
            raise HTTPException(status_code=400, detail=f"Missing required field: {k}")
        try:
            return float(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Field {k} must be numeric.")

    def _req_str(k: str) -> str:
        v = body.get(k)
        if v is None or str(v).strip() == "":
            raise HTTPException(status_code=400, detail=f"Missing required field: {k}")
        return str(v).strip()

    f = get_flags()
    ticker = _norm_ticker(body)
    timing = str(body.get("earningsTiming") or "").strip().upper()
    if timing not in ("BMO", "AMC", "UNK"):
        raise HTTPException(
            status_code=400,
            detail="earningsTiming must be BMO, AMC, or UNK.",
        )
    try:
        req = EarningsIcRequest(
            ticker=ticker,
            entry_date=_req_str("entryDate"),
            expiry=_req_str("expiry"),
            earnings_date=_req_str("earningsDate"),
            earnings_timing=timing,
            planned_exit_date=_req_str("plannedExitDate"),
            planned_exit_offset_hours=float(body.get("plannedExitOffsetHours") or 1.5),
            short_put=_req_float("shortPut"),
            long_put=_req_float("longPut"),
            short_call=_req_float("shortCall"),
            long_call=_req_float("longCall"),
            credit_received=_req_float("creditReceived"),
            profit_target_pct=float(
                body.get("profitTargetPct", f.ENGINE15_DEFAULT_PROFIT_TARGET_PCT)
            ),
            stop_loss_pct=float(body.get("stopLossPct", f.ENGINE15_DEFAULT_STOP_LOSS_PCT)),
            include_e1_payload=bool(body.get("includeE1Payload", True)),
            n_history=int(body.get("n") or 20),
            years_history=int(body.get("years") or 5),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid request: {type(e).__name__}: {e}")

    # Sanity checks — same as E14 plus a planned-exit bound.
    if not (req.long_put < req.short_put < req.short_call < req.long_call):
        raise HTTPException(
            status_code=400,
            detail="Strikes must satisfy: longPut < shortPut < shortCall < longCall.",
        )
    if req.credit_received <= 0:
        raise HTTPException(status_code=400, detail="creditReceived must be positive.")
    if req.entry_date >= req.expiry:
        raise HTTPException(status_code=400, detail="expiry must be after entryDate.")
    if req.planned_exit_date < req.entry_date:
        raise HTTPException(
            status_code=400,
            detail="plannedExitDate cannot be before entryDate.",
        )
    if req.planned_exit_date > req.expiry:
        raise HTTPException(
            status_code=400,
            detail="plannedExitDate cannot be after expiry.",
        )
    return req


def _cache_key(req) -> tuple:
    f = get_flags()
    return (
        req.ticker, req.entry_date, req.expiry,
        req.earnings_date, req.earnings_timing, req.planned_exit_date,
        round(float(req.planned_exit_offset_hours), 2),
        req.short_put, req.long_put, req.short_call, req.long_call,
        round(float(req.credit_received), 4),
        float(req.profit_target_pct), float(req.stop_loss_pct),
        f.cache_key_engine15(),
    )


@router.post("/api/earnings-ic/scenario")
def earnings_ic_scenario(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Run the Engine 15 earnings IC replay."""
    _ensure_enabled()
    req = _parse_scenario_body(body)
    key = _cache_key(req)

    with _SCENARIO_CACHE_LOCK:
        cached = _SCENARIO_CACHE.get(key)
    if cached is not None:
        return cached

    # Lazy import so the router stays importable when engine15 deps aren't warm.
    from backend.engine15.simulator import run_earnings_scenario

    try:
        client = get_client()
    except Exception as e:
        LOG.exception("ORATS client init failed")
        raise HTTPException(status_code=503, detail=f"ORATS client unavailable: {e}")

    bz = None
    try:
        bz = get_benzinga_client_optional()
    except Exception:
        bz = None
    store = None
    try:
        store = get_store_optional()
    except Exception:
        store = None

    try:
        result = run_earnings_scenario(
            req, client=client, benzinga_client=bz, store=store,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        LOG.exception("engine15: run_earnings_scenario failed")
        raise HTTPException(
            status_code=500,
            detail=f"Scenario replay failed: {type(e).__name__}: {e}",
        )

    with _SCENARIO_CACHE_LOCK:
        _SCENARIO_CACHE[key] = result
    return result


# ---------------------------------------------------------------------------
# /api/earnings-ic/pre-check — live NBBO & strike existence sanity
# ---------------------------------------------------------------------------

def _pre_check_block(kind: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {"severity": "block", "kind": kind, "message": message, **extra}


def _pre_check_warn(kind: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {"severity": "warn", "kind": kind, "message": message, **extra}


@router.post("/api/earnings-ic/pre-check")
def earnings_ic_pre_check(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Fast pre-submit guardrails before a /scenario call."""
    _ensure_enabled()
    ticker = _norm_ticker(body)
    expiry = str(body.get("expiry") or "").strip()
    if not expiry:
        raise HTTPException(status_code=400, detail="expiry is required.")

    def _req_float(k: str) -> float:
        v = body.get(k)
        if v is None or v == "":
            raise HTTPException(status_code=400, detail=f"Missing required field: {k}")
        try:
            return float(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Field {k} must be numeric.")

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

    blocks: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    try:
        client = get_client()
    except Exception as e:
        return {
            "ok": True,
            "blocks": [],
            "warnings": [
                _pre_check_warn(
                    "liveChainUnavailable",
                    f"Live chain unavailable ({type(e).__name__}); "
                    "proceeding without strike verification.",
                )
            ],
            "liveChain": None,
            "suggestion": None,
        }

    # Reuse engine14 live-chain helpers — they are ticker-agnostic.
    from backend.engine14.live_chain import fetch_live_chain_nbbo, validate_strikes_exist

    try:
        strike_check = validate_strikes_exist(
            client, ticker=ticker, expiry=expiry,
            short_put=short_put, long_put=long_put,
            short_call=short_call, long_call=long_call,
        )
    except Exception as e:
        LOG.warning("pre-check: strike validation failed: %s", e)
        strike_check = {"ok": False, "expiryFound": False, "missing": [], "availableStrikes": []}

    suggestion: Optional[Dict[str, Any]] = None
    if strike_check.get("expiryFound") and not strike_check.get("ok"):
        missing = strike_check.get("missing") or []
        blocks.append(_pre_check_block(
            "missingStrike",
            f"{len(missing)} leg(s) do not exist for {ticker} {expiry}.",
            missing=missing,
        ))
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
            f"No live chain data for {ticker} {expiry}. Strike existence not verified.",
        ))

    live_chain: Optional[Dict[str, Any]] = None
    if strike_check.get("ok"):
        try:
            live_chain = fetch_live_chain_nbbo(
                client, ticker=ticker, expiry=expiry,
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
                f"[${(net_bid or 0):.2f}, ${(net_ask or 0):.2f}] for mid ${mid:.2f}.",
                userCredit=credit,
                nbbo={"bid": net_bid, "ask": net_ask, "mid": mid},
            ))
        elif mid > 0 and abs(credit - mid) / mid > 0.25:
            warnings.append(_pre_check_warn(
                "creditFarFromMid",
                f"User credit ${credit:.2f} is >25% off live mid ${mid:.2f}.",
                userCredit=credit, mid=mid,
            ))

    # Coverage floor — tell the user when scenario will refuse to run.
    coverage = chain_cache.cache_coverage(ticker=ticker)
    f = get_flags()
    if int(coverage.get("daysCovered") or 0) < 2 * int(f.ENGINE15_MIN_EVENTS):
        warnings.append(_pre_check_warn(
            "thinChainCoverage",
            f"Chain cache for {ticker} has {coverage.get('daysCovered', 0)} days. "
            "Run /api/earnings-ic/backfill before scenario for best results.",
            coverage=coverage,
        ))

    return {
        "ok": len(blocks) == 0,
        "blocks": blocks,
        "warnings": warnings,
        "liveChain": live_chain,
        "availableStrikes": strike_check.get("availableStrikes") or [],
        "suggestion": suggestion,
        "coverage": coverage,
    }


# ---------------------------------------------------------------------------
# /api/earnings-ic/reconcile — live NBBO + E1 advisor sanity check
# ---------------------------------------------------------------------------

@router.post("/api/earnings-ic/reconcile")
def earnings_ic_reconcile(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Cross-check a scenario against a live NBBO snapshot and (optionally)
    the E1 LLM advisor. Deterministic-only by default (fast, sub-second)."""
    _ensure_enabled()

    scenario = body.get("scenario")
    if not isinstance(scenario, dict) or not scenario.get("entryState"):
        req = _parse_scenario_body(body.get("request") or body)
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
            from backend.engine15.simulator import run_earnings_scenario
            scenario = run_earnings_scenario(req, client=client, benzinga_client=bz, store=store)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            LOG.exception("reconcile: run_earnings_scenario failed")
            raise HTTPException(status_code=500, detail=f"Scenario replay failed: {type(e).__name__}: {e}")

    req_fields = scenario.get("request") or {}
    ticker = str(req_fields.get("ticker") or "").upper()
    expiry = str(req_fields.get("expiry") or "")

    live_chain: Optional[Dict[str, Any]] = None
    errors: Dict[str, str] = {}

    try:
        from backend.engine14.live_chain import fetch_live_chain_nbbo
        client = get_client()
        live_chain = fetch_live_chain_nbbo(
            client, ticker=ticker, expiry=expiry,
            short_put=float(req_fields.get("short_put")),
            long_put=float(req_fields.get("long_put")),
            short_call=float(req_fields.get("short_call")),
            long_call=float(req_fields.get("long_call")),
        )
    except Exception as e:
        LOG.warning("reconcile: live chain fetch failed: %s", e)
        errors["liveChain"] = f"{type(e).__name__}: {e}"

    # E1 desk consensus is cheap and deterministic — include for context.
    e1_summary = scenario.get("engine1Summary") or {}

    # Build a compact reconciliation view.
    user_credit = float(req_fields.get("credit_received") or 0.0)
    credit_chip: Dict[str, Any] = {"status": "unknown"}
    if live_chain:
        net_bid = live_chain.get("netBid")
        net_ask = live_chain.get("netAsk")
        mid = float(live_chain.get("mid") or 0.0)
        if net_bid is not None and user_credit < float(net_bid) - 1e-6:
            credit_chip = {"status": "mismatch", "note": f"User credit below live bid ${float(net_bid):.2f}."}
        elif net_ask is not None and user_credit > float(net_ask) + 1e-6:
            credit_chip = {"status": "mismatch", "note": f"User credit above live ask ${float(net_ask):.2f}."}
        elif mid > 0 and abs(user_credit - mid) / mid > 0.15:
            credit_chip = {
                "status": "drift",
                "note": f"User credit ${user_credit:.2f} is {100.0 * (user_credit - mid) / mid:+.0f}% vs live mid ${mid:.2f}.",
            }
        else:
            credit_chip = {"status": "match", "note": f"Credit inside live NBBO (mid ${mid:.2f})."}

    run_advisor = bool(body.get("runAdvisor", False))
    advisor_payload: Optional[Dict[str, Any]] = None
    if run_advisor:
        try:
            from backend.e15_earnings_scenario_advisor import generate_scenario_analysis
            advisor_payload = generate_scenario_analysis(
                engine1_payload=scenario.get("engine1") or {},
                scenario_payload=scenario,
            )
        except Exception as e:
            errors["advisor"] = f"{type(e).__name__}: {e}"

    return {
        "reconcile": {
            "creditChip": credit_chip,
            "engine1Consensus": e1_summary.get("deskConsensus"),
            "notes": scenario.get("notes") or [],
        },
        "scenario": scenario,
        "liveChain": live_chain,
        "advisor": advisor_payload,
        "errors": errors or None,
        "generatedAt": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# /api/earnings-ic/journal — reuse Engine 1 Redis store with source=engine15
# ---------------------------------------------------------------------------

@router.post("/api/earnings-ic/journal")
def earnings_ic_journal(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Persist a simulated earnings IC to the shared E1 trade journal."""
    _ensure_enabled()
    scenario = body.get("scenario") or {}
    req_fields = body.get("request") or scenario.get("request") or {}
    if not req_fields:
        raise HTTPException(status_code=400, detail="request payload missing.")

    from backend.e1_earnings_trades import log_trade

    ticker = str(req_fields.get("ticker") or "").upper()

    trade_entry = {
        "entryDate": req_fields.get("entry_date") or req_fields.get("entryDate"),
        "earningsDate": req_fields.get("earnings_date") or req_fields.get("earningsDate"),
        "earningsTiming": req_fields.get("earnings_timing") or req_fields.get("earningsTiming"),
        "plannedExitDate": req_fields.get("planned_exit_date") or req_fields.get("plannedExitDate"),
        "plannedExitOffsetHours": req_fields.get("planned_exit_offset_hours")
        or req_fields.get("plannedExitOffsetHours"),
        "expiry": req_fields.get("expiry"),
        "shortPutStrike": req_fields.get("short_put") or req_fields.get("shortPut"),
        "longPutStrike": req_fields.get("long_put") or req_fields.get("longPut"),
        "shortCallStrike": req_fields.get("short_call") or req_fields.get("shortCall"),
        "longCallStrike": req_fields.get("long_call") or req_fields.get("longCall"),
        "entryCredit": req_fields.get("credit_received") or req_fields.get("creditReceived"),
        "profitTargetPct": req_fields.get("profit_target_pct") or req_fields.get("profitTargetPct"),
        "stopLossPct": req_fields.get("stop_loss_pct") or req_fields.get("stopLossPct"),
        "impliedMovePct": ((scenario.get("entryState") or {}).get("userEmPct")),
        "spotAtEntry": ((scenario.get("entryState") or {}).get("userSpot")),
    }

    entry_context: Dict[str, Any] = {
        "engine15Scenario": scenario,
        "note": str(body.get("note") or "").strip() or None,
    }

    trade_data = {
        "ticker": ticker,
        "source": "engine15",
        "entry": trade_entry,
        "entryContext": entry_context,
        "advisorVerdict": {
            "engine": 15,
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
        "viewUrl": f"/earnings-ic?tradeId={trade_id}",
    }


@router.get("/api/earnings-ic/review")
def earnings_ic_review(trade_id: str = Query(..., alias="tradeId")) -> Dict[str, Any]:
    """Compare a stored engine15 simulation to its closed outcome, when present."""
    _ensure_enabled()
    from backend.e1_earnings_trades import get_trade

    trade = get_trade(trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found.")

    scenario = ((trade.get("entryContext") or {}).get("engine15Scenario")) or {}
    if not scenario:
        raise HTTPException(
            status_code=400,
            detail="This trade has no Engine 15 scenario attached — nothing to review.",
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
    actual: Dict[str, Any] = {
        "status": status,
        "closedAt": trade.get("closedAt"),
        "closeReason": trade.get("closeReason"),
    }
    if outcome:
        actual["pnlPct"] = outcome.get("pnlPct")
        actual["pnlDollars"] = outcome.get("pnlDollars")
        actual["daysHeld"] = outcome.get("daysHeld")

    verdict: Optional[str] = None
    if status == "closed" and actual.get("pnlPct") is not None:
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

    return {
        "tradeId": trade_id,
        "predicted": predicted,
        "predictedAdjusted": predicted_adj,
        "actual": actual,
        "verdict": verdict,
        "eventsUsed": scenario.get("eventsUsed"),
        "scenarioVersion": scenario.get("version"),
    }


# ---------------------------------------------------------------------------
# /api/earnings-ic/health + coverage
# ---------------------------------------------------------------------------

@router.get("/api/earnings-ic/health")
def earnings_ic_health(ticker: str = Query("", description="Optional ticker to probe")) -> Dict[str, Any]:
    f = get_flags()
    enabled = bool(getattr(f, "ENABLE_ENGINE15_EARNINGS_IC", False))
    t = (ticker or "").strip().upper()
    cov: Optional[Dict[str, Any]] = None
    if t:
        try:
            cov = chain_cache.cache_coverage(ticker=t)
        except Exception as e:
            cov = {"ticker": t, "daysCovered": 0, "error": f"{type(e).__name__}: {e}"}
    return {
        "enabled": enabled,
        "ticker": t or None,
        "coverage": cov,
        "minEvents": int(f.ENGINE15_MIN_EVENTS),
        "maxEvents": int(f.ENGINE15_MAX_EVENTS),
    }


@router.get("/api/earnings-ic/coverage")
def earnings_ic_coverage(ticker: str = Query(..., description="Ticker to inspect")) -> Dict[str, Any]:
    _ensure_enabled()
    t = ticker.strip().upper()
    if not t:
        raise HTTPException(status_code=400, detail="ticker is required.")
    return {t: chain_cache.cache_coverage(ticker=t)}


# ---------------------------------------------------------------------------
# /api/earnings-ic/backfill — admin, async per-ticker
# ---------------------------------------------------------------------------

def _run_backfill_bg(
    *,
    ticker: str,
    events: List[Dict[str, Any]],
    days_before: int,
    days_after: int,
    delay_ms: int,
) -> None:
    from backend.engine15 import chain_backfill

    def _set(**kw):
        with _BACKFILL_LOCK:
            st = _BACKFILL_STATE.setdefault(ticker, {})
            st.update(kw)

    def on_progress(progress: Dict[str, Any]) -> None:
        _set(progress=progress)

    try:
        client = get_client()
        result = chain_backfill.backfill_ticker_events(
            client,
            ticker=ticker,
            earnings_events=events,
            days_before=days_before,
            days_after=days_after,
            delay_ms=delay_ms,
            on_progress=on_progress,
        )
        _set(result=result, error=None)
    except Exception as e:
        LOG.exception("engine15 backfill failed for %s", ticker)
        _set(error=f"{type(e).__name__}: {e}")
    finally:
        _set(
            running=False,
            finished_at=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        )


@router.post("/api/earnings-ic/backfill")
def earnings_ic_backfill(
    body: Dict[str, Any] = Body(default_factory=dict),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
) -> Dict[str, Any]:
    """Kick off a background per-ticker earnings-event chain backfill."""
    _ensure_enabled()
    _check_admin_token(x_admin_token)

    ticker = _norm_ticker(body)
    f = get_flags()

    events: List[Dict[str, Any]]
    if isinstance(body.get("events"), list) and body["events"]:
        events = list(body["events"])
    else:
        # Harvest from a fresh Engine 1 scan.
        try:
            e1 = _run_engine1(
                ticker,
                n=int(body.get("n") or f.ENGINE15_MAX_EVENTS),
                years=int(body.get("years") or 5),
            )
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Engine 1 scan failed while harvesting events: {type(e).__name__}: {e}",
            )
        events = list(e1.get("events") or [])

    if not events:
        raise HTTPException(
            status_code=400,
            detail=f"No earnings events available for {ticker}.",
        )

    days_before = int(body.get("daysBefore") or f.ENGINE15_EVENT_BACKFILL_DAYS_BEFORE)
    days_after = int(body.get("daysAfter") or f.ENGINE15_EVENT_BACKFILL_DAYS_AFTER)
    delay_ms = int(body.get("delayMs") or f.ENGINE15_BACKFILL_DELAY_MS)

    with _BACKFILL_LOCK:
        cur = _BACKFILL_STATE.get(ticker) or {}
        if cur.get("running"):
            raise HTTPException(status_code=409, detail=f"Backfill already in progress for {ticker}.")
        _BACKFILL_STATE[ticker] = {
            "ticker": ticker,
            "running": True,
            "started_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
            "finished_at": None,
            "progress": None,
            "error": None,
            "params": {
                "events": len(events),
                "daysBefore": days_before,
                "daysAfter": days_after,
                "delayMs": delay_ms,
            },
            "result": None,
        }

    _BACKFILL_EXECUTOR.submit(
        _run_backfill_bg,
        ticker=ticker,
        events=events,
        days_before=days_before,
        days_after=days_after,
        delay_ms=delay_ms,
    )

    with _BACKFILL_LOCK:
        state = dict(_BACKFILL_STATE[ticker])
    return {"started": True, "state": state}


@router.get("/api/earnings-ic/backfill/status")
def earnings_ic_backfill_status(ticker: str = Query(..., description="Ticker to inspect")) -> Dict[str, Any]:
    _ensure_enabled()
    t = ticker.strip().upper()
    if not t:
        raise HTTPException(status_code=400, detail="ticker is required.")
    with _BACKFILL_LOCK:
        state = dict(_BACKFILL_STATE.get(t) or {})
    state["coverage"] = chain_cache.cache_coverage(ticker=t)
    return state


# ---------------------------------------------------------------------------
# /api/earnings-ic/explain-card — per-card LLM tooltips
# ---------------------------------------------------------------------------

@router.get("/api/earnings-ic/explain-card/catalog")
def earnings_ic_explain_catalog() -> Dict[str, Any]:
    _ensure_enabled()
    from backend.engine15.card_explain import CARD_CATALOG, supported_card_types
    return {
        "cardTypes": supported_card_types(),
        "titles": {k: v.get("title", k) for k, v in CARD_CATALOG.items()},
    }


@router.post("/api/earnings-ic/explain-card")
def earnings_ic_explain_card(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    _ensure_enabled()
    from backend.engine15.card_explain import (
        CARD_CATALOG,
        generate_card_explanation,
        supported_card_types,
    )
    card_type = str(body.get("cardType") or "").strip()
    if not card_type:
        raise HTTPException(status_code=400, detail="cardType is required.")
    if card_type not in CARD_CATALOG:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown cardType {card_type!r}. Valid: {', '.join(supported_card_types())}",
        )
    return generate_card_explanation(
        card_type=card_type,
        card_data=body.get("cardData") or {},
        scenario_context=body.get("scenarioContext") or {},
    )


# ---------------------------------------------------------------------------
# /api/earnings-ic/advisor — LLM desk narrative
# ---------------------------------------------------------------------------

@router.post("/api/earnings-ic/advisor")
def earnings_ic_advisor(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Single LLM call over the combined E1 + replay payload. Returns a
    structured JSON verdict similar in shape to the Engine 1 advisor."""
    _ensure_enabled()
    scenario = body.get("scenario") or {}
    if not scenario.get("request"):
        raise HTTPException(status_code=400, detail="scenario payload missing.")
    engine1_payload = body.get("engine1") or scenario.get("engine1") or {}

    try:
        from backend.e15_earnings_scenario_advisor import generate_scenario_analysis
        return generate_scenario_analysis(
            engine1_payload=engine1_payload,
            scenario_payload=scenario,
        )
    except Exception as e:
        LOG.exception("engine15 advisor failed")
        raise HTTPException(
            status_code=500,
            detail=f"Advisor failed: {type(e).__name__}: {e}",
        )
