"""Trade Memory System — shared helpers for rich trade context capture.

Provides market snapshot, outcome tagging, and pattern detection utilities
used by both Engine 1 (earnings IC) and Engine 2 (SPX IC) trade systems.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Market snapshot — captures the full market environment at a point in time
# ---------------------------------------------------------------------------

def capture_market_snapshot(
    *,
    store: Any = None,
    orats_client: Any = None,
    ticker: str = "SPY",
) -> Dict[str, Any]:
    """Capture a comprehensive market state snapshot for trade context.

    Pulls from: Engine 5 regime, vol surface, macro proximity, consensus,
    ORATS live data. Degrades gracefully — returns whatever is available.
    """
    snap: Dict[str, Any] = {"capturedAt": dt.datetime.utcnow().isoformat() + "Z"}

    # Regime from Engine 5 snapshot
    if store is not None:
        try:
            from backend.engine5_snapshot import select_best_snapshot
            from backend.config import get_flags
            flags = get_flags()
            e5 = select_best_snapshot(
                store,
                max_age_days=flags.ENGINE5_SNAPSHOT_BEST_MAX_AGE_DAYS,
                snapshot_ttl=flags.ENGINE5_SNAPSHOT_TTL_S,
            )
            if e5:
                regime = e5.get("data", {}).get("regime", {})
                snap["regimeLabel"] = regime.get("label")
                snap["regimeScore"] = regime.get("score")
                snap["regimeComponents"] = regime.get("components")
                snap["smallCapBias"] = regime.get("small_cap_bias")
        except Exception as exc:
            LOG.debug("Market snapshot: regime unavailable: %s", exc)

    # VIX / IV from ORATS
    if orats_client is not None:
        try:
            resp = orats_client.live_summaries(ticker=ticker)
            rows = resp.rows or []
            if rows:
                r0 = rows[0]
                snap["vixLevel"] = r0.get("iv30dMean") or r0.get("ivMean")
                snap["ivRank"] = r0.get("ivRank")
                snap["stockPrice"] = r0.get("stockPrice")
        except Exception as exc:
            LOG.debug("Market snapshot: ORATS unavailable: %s", exc)

    # Macro proximity
    try:
        from backend.macro_calendar_engine import get_macro_proximity
        from backend.deps import get_benzinga_client_optional
        bz = get_benzinga_client_optional()
        macro = get_macro_proximity(benzinga_client=bz, store=store)
        snap["macroMultiplier"] = macro.multiplier
        snap["macroRiskLevel"] = macro.risk_level
        snap["macroFlags"] = macro.flags
        snap["macroEventCount"] = len(macro.events)
    except Exception as exc:
        LOG.debug("Market snapshot: macro unavailable: %s", exc)

    # Cross-asset stress from DMS
    if store is not None:
        try:
            from backend.daily_market_state import load_dms
            dms = load_dms(dt.date.today().isoformat(), store=store)
            if dms:
                d = dms if isinstance(dms, dict) else dms.to_dict() if hasattr(dms, "to_dict") else {}
                cas = d.get("cross_asset_stress", {})
                snap["crossAssetComposite"] = cas.get("composite_score")
                snap["crossAssetLabel"] = cas.get("label")
        except Exception as exc:
            LOG.debug("Market snapshot: DMS unavailable: %s", exc)

    # Vol surface
    if orats_client is not None:
        try:
            from backend.vol_surface_engine import get_vol_surface
            surface = get_vol_surface(orats_client, ticker=ticker, store=store, cache_ttl_s=120)
            snap["volSurfaceSkew"] = surface.skew_25d
            snap["volSurfaceSkewLabel"] = surface.skew_label
            snap["termStructureSlope"] = surface.term_structure_slope
            snap["termStructureLabel"] = surface.term_structure_label
        except Exception as exc:
            LOG.debug("Market snapshot: vol surface unavailable: %s", exc)

    # Consensus
    try:
        from backend.consensus_engine import build_consensus_from_apis
        regime_data = None
        if store is not None:
            try:
                from backend.engine5_snapshot import select_best_snapshot as _sel
                from backend.config import get_flags as _gf
                _fl = _gf()
                _snap = _sel(store, max_age_days=_fl.ENGINE5_SNAPSHOT_BEST_MAX_AGE_DAYS,
                             snapshot_ttl=_fl.ENGINE5_SNAPSHOT_TTL_S)
                if _snap:
                    regime_data = _snap.get("data", {}).get("regime", {})
            except Exception:
                pass
        consensus = build_consensus_from_apis(regime_data=regime_data)
        snap["consensusDirection"] = consensus.consensus_direction
        snap["consensusScore"] = consensus.consensus_score
    except Exception as exc:
        LOG.debug("Market snapshot: consensus unavailable: %s", exc)

    return snap


# ---------------------------------------------------------------------------
# Outcome tagging — auto-detect trade characteristics for learning
# ---------------------------------------------------------------------------

AUTO_TAGS_E2 = {
    "near_miss": "Breach proximity exceeded 70% at some point during the trade",
    "max_pain": "Loss exceeded 2x the average loss in the performance digest",
    "expired_worthless": "IC expired OTM with full credit retained",
    "rolled": "Position was rolled during the trade",
    "early_exit": "Closed before expiry with DTE remaining",
    "survived_stress": "Regime shifted to Stressed during the trade but IC survived",
}

USER_TAGS = [
    "double_down_next_time",
    "avoid_this_setup",
    "size_up",
    "size_down",
    "regime_mismatch",
    "macro_surprise",
    "thesis_validated",
    "got_lucky",
]


def compute_auto_tags_e2(trade: Dict[str, Any]) -> List[str]:
    """Compute automatic outcome tags for an Engine 2 trade."""
    tags: List[str] = []
    outcome = trade.get("outcome", {}) or {}
    checkins = trade.get("checkIns", []) or []

    if outcome.get("expiredWorthless"):
        tags.append("expired_worthless")

    max_breach_prox = 0.0
    for ci in checkins:
        tracking = ci.get("tracking", {}) or {}
        bp_put = float(tracking.get("breachProximityPut", 0) or 0)
        bp_call = float(tracking.get("breachProximityCall", 0) or 0)
        max_breach_prox = max(max_breach_prox, bp_put, bp_call)
    if max_breach_prox > 70:
        tags.append("near_miss")

    if trade.get("closeReason") == "rolled":
        tags.append("rolled")

    entry_date = trade.get("entry", {}).get("entryDate") or trade.get("loggedAt", "")[:10]
    expiry = trade.get("entry", {}).get("expiryDate", "")
    close_date = (trade.get("closedAt") or "")[:10]
    if expiry and close_date and close_date < expiry:
        tags.append("early_exit")

    regime_at_entry = (trade.get("entryContext", {}) or {}).get("regimeBucket", "")
    regime_shifted = False
    for ci in checkins:
        tracking = ci.get("tracking", {}) or {}
        drift = tracking.get("regimeDriftBucket")
        if drift and drift != regime_at_entry:
            regime_shifted = True
    if regime_shifted and outcome.get("outcomeClass") == "win":
        tags.append("survived_stress")

    return tags


def compute_auto_tags_e1(trade: Dict[str, Any]) -> List[str]:
    """Compute automatic outcome tags for an Engine 1 earnings trade."""
    tags: List[str] = []
    outcome = trade.get("outcome", {}) or {}

    if outcome.get("expiredWorthless"):
        tags.append("expired_worthless")
    if outcome.get("breachOccurred"):
        tags.append("breach_occurred")

    move_vs_predicted = outcome.get("moveVsPredicted")
    if move_vs_predicted is not None:
        if float(move_vs_predicted) < 0.5:
            tags.append("massive_vol_crush")
        elif float(move_vs_predicted) > 1.5:
            tags.append("move_exceeded_implied")

    hold_decision = outcome.get("holdDecision")
    if hold_decision and "hold" in str(hold_decision).lower():
        tags.append("held_past_open")

    return tags


# ---------------------------------------------------------------------------
# Pattern detection — mine the trade corpus for actionable insights
# ---------------------------------------------------------------------------

def detect_patterns(
    closed_trades: List[Dict[str, Any]],
    *,
    engine: str = "e2",
) -> List[str]:
    """Detect actionable patterns from closed trades for the LLM journal.

    Returns a list of human-readable insight strings.
    """
    if len(closed_trades) < 5:
        return []

    insights: List[str] = []

    # Win rate by VIX bucket at entry
    vix_buckets: Dict[str, List[str]] = {}
    for t in closed_trades:
        ms = t.get("marketSnapshot", {}) or {}
        vix = ms.get("vixLevel")
        oc = (t.get("outcome", {}) or {}).get("outcomeClass")
        if vix is not None and oc:
            bucket = "<18" if float(vix) < 0.18 else "18-22" if float(vix) < 0.22 else ">22"
            vix_buckets.setdefault(bucket, []).append(oc)
    for bucket, outcomes in sorted(vix_buckets.items()):
        n = len(outcomes)
        if n >= 3:
            w = sum(1 for o in outcomes if o == "win")
            wr = round(w / n * 100)
            if wr >= 80:
                insights.append(f"Trades with VIX {bucket} are {w}-{n - w} (win rate {wr}%). Strong edge here.")
            elif wr <= 40:
                insights.append(f"Trades with VIX {bucket} are {w}-{n - w} (win rate {wr}%). Consider sizing down.")

    # Verdict calibration
    verdicts: Dict[str, Dict[str, int]] = {}
    for t in closed_trades:
        v = (t.get("advisorVerdict") or {}).get("verdict", "?")
        oc = (t.get("outcome", {}) or {}).get("outcomeClass")
        if oc:
            verdicts.setdefault(v, {"win": 0, "loss": 0, "total": 0})
            verdicts[v]["total"] += 1
            if oc == "win":
                verdicts[v]["win"] += 1
            elif oc == "loss":
                verdicts[v]["loss"] += 1
    for v, counts in verdicts.items():
        if counts["total"] >= 3:
            wr = round(counts["win"] / counts["total"] * 100)
            if v == "LEAN_PASS" and wr >= 70:
                insights.append(f"LEAN_PASS verdicts converted to wins {wr}% of the time — consider upgrading to TRADE.")
            elif v == "TRADE" and wr <= 40:
                insights.append(f"TRADE verdicts only won {wr}% — the bar may need to be higher.")

    # Engine 1 specific: VRP calibration
    if engine == "e1":
        vrp_buckets: Dict[str, List[float]] = {}
        for t in closed_trades:
            ctx = t.get("entryContext", {}) or {}
            vrp = ctx.get("vrpScore")
            move_ratio = (t.get("outcome", {}) or {}).get("moveVsPredicted")
            if vrp is not None and move_ratio is not None:
                bucket = "75+" if float(vrp) >= 75 else "60-75" if float(vrp) >= 60 else "<60"
                vrp_buckets.setdefault(bucket, []).append(float(move_ratio))
        for bucket, ratios in sorted(vrp_buckets.items()):
            if len(ratios) >= 3:
                avg = round(sum(ratios) / len(ratios), 2)
                insights.append(f"VRP {bucket} trades: avg actual/predicted move ratio = {avg}.")

    # Timing patterns (E1: AMC vs BMO)
    if engine == "e1":
        timing: Dict[str, List[float]] = {}
        for t in closed_trades:
            ctx = t.get("entryContext", {}) or {}
            tm = str(ctx.get("earningsTiming", "")).upper()
            pnl = (t.get("outcome", {}) or {}).get("realizedPnl")
            if tm in ("AMC", "BMO") and pnl is not None:
                timing.setdefault(tm, []).append(float(pnl))
        for tm, pnls in timing.items():
            if len(pnls) >= 3:
                avg = round(sum(pnls) / len(pnls), 2)
                other = "BMO" if tm == "AMC" else "AMC"
                other_pnls = timing.get(other, [])
                if other_pnls:
                    other_avg = round(sum(other_pnls) / len(other_pnls), 2)
                    diff = round(avg - other_avg, 2)
                    if abs(diff) > 0.20:
                        better = tm if diff > 0 else other
                        insights.append(f"{better} trades outperform by ${abs(diff):.2f} avg P&L.")

    # Win/loss streak
    recent_outcomes = []
    for t in sorted(closed_trades, key=lambda x: x.get("closedAt", ""))[-20:]:
        oc = (t.get("outcome", {}) or {}).get("outcomeClass")
        if oc in ("win", "loss"):
            recent_outcomes.append(oc)
    if len(recent_outcomes) >= 3:
        streak = 1
        for i in range(len(recent_outcomes) - 2, -1, -1):
            if recent_outcomes[i] == recent_outcomes[-1]:
                streak += 1
            else:
                break
        if streak >= 3:
            label = recent_outcomes[-1]
            insights.append(f"Current {label} streak: {streak} trades. {'Momentum is strong.' if label == 'win' else 'Review setups — possible tilt.'}")

    return insights


# ---------------------------------------------------------------------------
# Trade archive — periodic Redis-to-JSON backup
# ---------------------------------------------------------------------------

def archive_trades_to_json(
    trades: List[Dict[str, Any]],
    engine: str = "e2",
    archive_dir: str = "",
) -> Optional[str]:
    """Write a JSON backup of trade documents to the archive directory.

    Returns the path written, or None on failure.
    """
    if not archive_dir:
        archive_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "data", "trade_archive",
        )
    os.makedirs(archive_dir, exist_ok=True)

    today = dt.date.today().isoformat()
    filename = f"{engine}_trades_{today}.json"
    path = os.path.join(archive_dir, filename)

    try:
        with open(path, "w") as f:
            json.dump(trades, f, indent=2, default=str)
        LOG.info("Trade archive written: %s (%d trades)", path, len(trades))
        return path
    except Exception as exc:
        LOG.warning("Trade archive failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Trade-log enrichment — ensures the predicted breach probability lands in
# entryContext.breachPct on every advisor-sourced trade. Without this, the
# v2 conformal calibrator can't observe the (prediction, realized) pair.
# ---------------------------------------------------------------------------


def derive_breach_pct(trade_data: Dict[str, Any]) -> tuple[Optional[float], str]:
    """Search a trade payload for the predicted breach probability.

    Mirrors the exact logic used by the v2 v1_mirror so server-side
    enrichment and the v2 backfill stay in sync. All v1 values live on
    a 0..100 percent scale; we keep that scale here.

    Returns ``(breach_pct, source_label)``. ``breach_pct`` is ``None`` if
    no path produced an in-range value; ``source_label`` is e.g.
    ``"breachSnapshot.breachRatePct"`` for diagnostics.

    Order of preference:
      1. entryContext.breachPct           (already-canonical; nothing to do)
      2. breachSnapshot.breachRatePct
      3. breachSnapshot.breachRate
      4. breachSnapshot.breachPct
      5. breachSnapshot.emBreachPct
      6. predictionSnapshot.breachProb     (E1)
      7. predictionSnapshot.breachPct
      8. entry.emBreachPct                 (some E1 advisor paths)
      9. entry.breachPct
     10. entryContext.emBreachSummary[min] (E1 EM-bucketed → tightest wing)
     11. marketSnapshot.breachPct          (rare fallback)
    """
    candidates: List[tuple[str, Any]] = [
        ("entryContext.breachPct", _safe_get(trade_data, "entryContext", "breachPct")),
        ("breachSnapshot.breachRatePct", _safe_get(trade_data, "breachSnapshot", "breachRatePct")),
        ("breachSnapshot.breachRate", _safe_get(trade_data, "breachSnapshot", "breachRate")),
        ("breachSnapshot.breachPct", _safe_get(trade_data, "breachSnapshot", "breachPct")),
        ("breachSnapshot.emBreachPct", _safe_get(trade_data, "breachSnapshot", "emBreachPct")),
        ("predictionSnapshot.breachProb", _safe_get(trade_data, "predictionSnapshot", "breachProb")),
        ("predictionSnapshot.breachPct", _safe_get(trade_data, "predictionSnapshot", "breachPct")),
        ("entry.emBreachPct", _safe_get(trade_data, "entry", "emBreachPct")),
        ("entry.breachPct", _safe_get(trade_data, "entry", "breachPct")),
        ("entryContext.emBreachSummary[min]", _min_of_dict_values(_safe_get(trade_data, "entryContext", "emBreachSummary"))),
        ("marketSnapshot.breachPct", _safe_get(trade_data, "marketSnapshot", "breachPct")),
    ]
    for label, raw in candidates:
        try:
            if raw is None:
                continue
            v = float(raw)
        except (TypeError, ValueError):
            continue
        # predictionSnapshot.breachProb is on [0, 1]; everything else is [0, 100].
        if label == "predictionSnapshot.breachProb" and 0.0 <= v <= 1.0:
            return round(v * 100.0, 4), label
        if 0.0 <= v <= 100.0:
            return round(v, 4), label
    return None, "none"


def enrich_trade_log_payload(
    trade_data: Dict[str, Any],
    *,
    engine: str = "unknown",
) -> Dict[str, Any]:
    """Ensure entryContext.breachPct is populated before the trade is persisted.

    Called from log_trade in both engine1 and engine2. Mutates the
    ``trade_data`` dict in place AND returns it for chaining. Logs a
    WARN if no prediction source could be found so we surface FE/advisor
    payload bugs immediately rather than discovering them weeks later
    when the conformal mirror runs.
    """
    ctx = trade_data.setdefault("entryContext", {})
    if ctx.get("breachPct") is not None:
        return trade_data  # already canonical, nothing to do.

    breach_pct, source = derive_breach_pct(trade_data)
    if breach_pct is None:
        LOG.warning(
            "trade_log enrichment[%s]: no breach prediction found in payload "
            "(checked entryContext / breachSnapshot / predictionSnapshot / "
            "entry / marketSnapshot). v2 conformal calibrator will skip this trade.",
            engine,
        )
        return trade_data

    ctx["breachPct"] = breach_pct
    ctx.setdefault("breachPctSource", source)
    LOG.info(
        "trade_log enrichment[%s]: breachPct=%.2f%% derived from %s",
        engine, breach_pct, source,
    )
    return trade_data


def _safe_get(doc: Any, *path: str) -> Any:
    cur: Any = doc
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _min_of_dict_values(d: Any) -> Optional[float]:
    if not isinstance(d, dict) or not d:
        return None
    vals: List[float] = []
    for v in d.values():
        try:
            f = float(v)
            if 0.0 <= f <= 100.0:
                vals.append(f)
        except (TypeError, ValueError):
            continue
    return min(vals) if vals else None
