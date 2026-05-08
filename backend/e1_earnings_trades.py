"""Engine 1 — Earnings IC Trade Persistence (Redis).

Redis key layout:
    e1:trades:{trade_id}    — individual trade JSON document
    e1:trades:index         — ordered list of trade IDs (newest last)

Cross-ticker learning: the performance digest buckets by VRP score,
breach rate, EM, wing width, AMC/BMO timing, and regime so the LLM
can learn patterns that transfer across names.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import statistics
import uuid
from typing import Any, Dict, List, Optional

from backend.config import get_flags, FeatureFlags
from backend.redis_store import RedisStore, get_store_optional

LOG = logging.getLogger(__name__)

_PREFIX = "e1:trades"
_INDEX_KEY = f"{_PREFIX}:index"


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _trade_key(trade_id: str) -> str:
    return f"{_PREFIX}:{trade_id}"


def _refresh_index_ttl(store: RedisStore) -> None:
    """Touch the index key's TTL without modifying its contents.

    Called from close_trade, add_checkin, and set_post_mortem so the index
    TTL stays aligned with individual trade key TTLs.
    """
    ttl = int(get_flags().E1_TRADE_TTL_S)
    index = store.get_json(_INDEX_KEY)
    if index is not None:
        store.set_json(_INDEX_KEY, index, ttl_s=ttl)


def rebuild_index_if_missing(
    store: Optional[RedisStore] = None,
) -> bool:
    """Rebuild the trade index from existing keys if it has expired.

    Uses SCAN to discover orphaned trade keys and reconstructs the index.
    Returns True if a rebuild was performed, False otherwise.
    """
    s = store or get_store_optional()
    if s is None:
        return False
    existing = s.get_json(_INDEX_KEY)
    if existing is not None:
        return False
    keys = s.scan_keys(f"{_PREFIX}:*")
    if not keys:
        return False
    trade_ids = sorted(
        k.replace(f"{_PREFIX}:", "")
        for k in keys
        if k != _INDEX_KEY
    )
    if not trade_ids:
        return False
    f = get_flags()
    ttl = int(f.E1_TRADE_TTL_S)
    max_idx = int(f.E1_TRADE_MAX_INDEX)
    s.set_json(_INDEX_KEY, trade_ids[-max_idx:], ttl_s=ttl)
    LOG.info("E1 trades: rebuilt index from %d keys", len(trade_ids))
    return True


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def log_trade(
    trade_data: Dict[str, Any],
    store: Optional[RedisStore] = None,
    flags: Optional[FeatureFlags] = None,
) -> Optional[str]:
    """Persist a new earnings IC trade to Redis. Returns trade_id or None."""
    s = store or get_store_optional()
    if s is None:
        LOG.warning("E1 trade log: Redis unavailable")
        return None

    f = flags or get_flags()
    trade_id = str(uuid.uuid4())[:12]
    source = str(trade_data.get("source", "advisor"))

    # Enrich entryContext.breachPct from breachSnapshot/predictionSnapshot if
    # the FE didn't set it. This is what the v2 conformal calibrator reads.
    try:
        from backend.trade_memory import enrich_trade_log_payload
        enrich_trade_log_payload(trade_data, engine="e1")
    except Exception as exc:
        LOG.debug("E1 trade enrichment failed (non-fatal): %s", exc)

    trade: Dict[str, Any] = {
        "tradeId": trade_id,
        "status": "active",
        "loggedAt": _utcnow_iso(),
        "source": source,
        "ticker": str(trade_data.get("ticker", "")).upper(),
        "entry": trade_data.get("entry", {}),
        "entryContext": trade_data.get("entryContext", {}),
        "marketSnapshot": trade_data.get("marketSnapshot", {}),
        "vrpSnapshot": trade_data.get("vrpSnapshot", {}),
        "breachSnapshot": trade_data.get("breachSnapshot", {}),
        "predictionSnapshot": trade_data.get("predictionSnapshot", {}),
        "advisorVerdict": trade_data.get("advisorVerdict"),
        "checkIns": [],
    }

    if source == "adjusted":
        trade["originalTicket"] = trade_data.get("originalTicket")
        trade["adjustmentNote"] = trade_data.get("adjustmentNote")

    ttl = int(f.E1_TRADE_TTL_S)
    try:
        s.set_json(_trade_key(trade_id), trade, ttl_s=ttl)
        _append_index(s, trade_id, max_len=int(f.E1_TRADE_MAX_INDEX), ttl=ttl)
        return trade_id
    except Exception as e:
        LOG.warning("E1 trade log failed: %s", e)
        return None


def close_trade(
    trade_id: str,
    close_data: Optional[Dict[str, Any]] = None,
    store: Optional[RedisStore] = None,
) -> Optional[Dict[str, Any]]:
    """Close an active trade with outcome data."""
    s = store or get_store_optional()
    if s is None:
        return None

    trade = get_trade(trade_id, store=s)
    if trade is None:
        return None

    cd = close_data or {}
    entry_credit = float(trade.get("entry", {}).get("entryCredit", 0) or 0)
    exit_credit = cd.get("exitCredit")
    realized_pnl = None
    if exit_credit is not None:
        realized_pnl = entry_credit - float(exit_credit)

    outcome_class = "scratch"
    if realized_pnl is not None:
        if realized_pnl > 0.05:
            outcome_class = "win"
        elif realized_pnl < -0.05:
            outcome_class = "loss"

    trade["status"] = "closed"
    trade["closedAt"] = _utcnow_iso()
    trade["closeReason"] = cd.get("closeReason", "manual")

    # Realized move vs predicted move (the core VRP metric)
    actual_move = cd.get("actualMove")
    predicted_move = float(trade.get("entry", {}).get("impliedMovePct", 0) or 0)
    move_vs_predicted = None
    if actual_move is not None and predicted_move > 0:
        move_vs_predicted = round(float(actual_move) / predicted_move, 3)

    # Breach detection from check-ins or close data
    breach_occurred = bool(cd.get("breachOccurred", False))
    if not breach_occurred:
        for ci in trade.get("checkIns", []):
            if ci.get("breachOccurred"):
                breach_occurred = True
                break

    # Hold duration
    hold_duration_days = None
    entry_date_str = trade.get("entry", {}).get("entryDate") or (trade.get("loggedAt") or "")[:10]
    close_date_str = trade["closedAt"][:10]
    try:
        ed = dt.date.fromisoformat(entry_date_str)
        cd_date = dt.date.fromisoformat(close_date_str)
        hold_duration_days = (cd_date - ed).days
    except Exception:
        pass

    # Auto outcome tags
    auto_tags: list = []
    try:
        from backend.trade_memory import compute_auto_tags_e1
        trade["outcome"] = {
            "expiredWorthless": bool(cd.get("expiredWorthless", False)),
            "breachOccurred": breach_occurred,
            "moveVsPredicted": move_vs_predicted,
            "holdDecision": cd.get("holdDecision"),
        }
        auto_tags = compute_auto_tags_e1(trade)
    except Exception:
        pass
    user_tags = cd.get("outcomeTags", [])

    trade["outcome"] = {
        "entryCredit": entry_credit,
        "exitCredit": float(exit_credit) if exit_credit is not None else None,
        "realizedPnl": float(realized_pnl) if realized_pnl is not None else None,
        "outcomeClass": outcome_class,
        "expiredWorthless": bool(cd.get("expiredWorthless", False)),
        "notes": cd.get("notes"),
        "actualMove": float(actual_move) if actual_move is not None else None,
        "moveVsPredicted": move_vs_predicted,
        "breachOccurred": breach_occurred,
        "holdDecision": cd.get("holdDecision"),
        "holdDeltaPnl": cd.get("holdDeltaPnl"),
        "holdDurationDays": hold_duration_days,
        "spotAtExit": cd.get("spotAtExit"),
        "vixAtExit": cd.get("vixAtExit"),
        "autoTags": auto_tags,
        "userTags": user_tags if isinstance(user_tags, list) else [],
    }

    try:
        ttl = int(get_flags().E1_TRADE_TTL_S)
        s.set_json(_trade_key(trade_id), trade, ttl_s=ttl)
        _refresh_index_ttl(s)
        return trade
    except Exception as e:
        LOG.warning("E1 close trade failed: %s", e)
        return None


def get_trade(trade_id: str, store: Optional[RedisStore] = None) -> Optional[Dict[str, Any]]:
    s = store or get_store_optional()
    if s is None:
        return None
    return s.get_json(_trade_key(trade_id))


def list_active_trades(store: Optional[RedisStore] = None) -> List[Dict[str, Any]]:
    return [t for t in _list_all(store) if t.get("status") in ("active", "monitoring")]


def list_closed_trades(store: Optional[RedisStore] = None, limit: int = 100) -> List[Dict[str, Any]]:
    closed = [t for t in _list_all(store) if t.get("status") == "closed"]
    closed.sort(key=lambda t: t.get("closedAt", ""), reverse=True)
    return closed[:limit]


def set_post_mortem(
    trade_id: str,
    post_mortem: Dict[str, Any],
    store: Optional[RedisStore] = None,
) -> bool:
    """Attach a post-mortem to a closed trade."""
    s = store or get_store_optional()
    if s is None:
        return False
    trade = get_trade(trade_id, store=s)
    if trade is None or trade.get("status") != "closed":
        return False
    trade["postMortem"] = post_mortem
    try:
        ttl = int(get_flags().E1_TRADE_TTL_S)
        s.set_json(_trade_key(trade_id), trade, ttl_s=ttl)
        _refresh_index_ttl(s)
        LOG.info("E1 post-mortem set for %s category=%s", trade_id, post_mortem.get("category"))
        return True
    except Exception:
        return False


def add_checkin(
    trade_id: str,
    checkin_data: Dict[str, Any],
    store: Optional[RedisStore] = None,
) -> bool:
    s = store or get_store_optional()
    if s is None:
        return False
    trade = get_trade(trade_id, store=s)
    if trade is None:
        return False

    checkins = trade.get("checkIns") or []
    checkin_data["timestamp"] = _utcnow_iso()
    checkins.append(checkin_data)
    trade["checkIns"] = checkins[-20:]

    try:
        ttl = int(get_flags().E1_TRADE_TTL_S)
        s.set_json(_trade_key(trade_id), trade, ttl_s=ttl)
        _refresh_index_ttl(s)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Cross-Ticker Performance Digest
# ---------------------------------------------------------------------------

def compute_e1_trade_performance_digest(
    store: Optional[RedisStore] = None,
) -> Dict[str, Any]:
    """Aggregate closed-trade performance with cross-ticker bucketing.

    Unlike Engine 2 (always SPX), Engine 1 trades different names each time.
    The digest buckets by profile characteristics that transfer across tickers:
    VRP score bucket, breach rate bucket, EM, wing, AMC/BMO timing, regime.

    Engine 15 trades (source="engine15") are excluded: they live in the same
    Redis namespace for convenience but have a different entry schema (no
    ``emMultiple`` / ``wingWidth`` in the E1 sense), and mixing them would
    pollute the E1 learning journal's cross-ticker buckets.
    """
    closed = [
        t for t in list_closed_trades(store=store, limit=200)
        if str(t.get("source") or "").lower() != "engine15"
    ][:100]
    if not closed:
        return {"totalClosed": 0, "hasData": False}

    wins, losses, scratches = 0, 0, 0
    pnl_list: List[float] = []
    em_buckets: Dict[str, List[Dict[str, Any]]] = {}
    wing_buckets: Dict[str, List[Dict[str, Any]]] = {}
    regime_buckets: Dict[str, List[Dict[str, Any]]] = {}
    vrp_buckets: Dict[str, List[Dict[str, Any]]] = {}
    breach_buckets: Dict[str, List[Dict[str, Any]]] = {}
    timing_buckets: Dict[str, List[Dict[str, Any]]] = {}
    verdict_outcomes: Dict[str, Dict[str, int]] = {}

    for t in closed:
        outcome = t.get("outcome") or {}
        oc = outcome.get("outcomeClass")
        pnl = outcome.get("realizedPnl")
        entry = t.get("entry", {})
        ctx = t.get("entryContext", {})
        advisor = t.get("advisorVerdict") or {}

        if oc == "win":
            wins += 1
        elif oc == "loss":
            losses += 1
        elif oc == "scratch":
            scratches += 1

        if pnl is not None:
            pnl_list.append(float(pnl))

        rec = {"outcomeClass": oc, "pnl": pnl, "ticker": t.get("ticker")}

        # EM bucket
        em_key = str(entry.get("emMultiple", "?"))
        em_buckets.setdefault(em_key, []).append(rec)

        # Wing bucket
        wing_key = f"${entry.get('wingWidth', '?')}"
        wing_buckets.setdefault(wing_key, []).append(rec)

        # Regime bucket
        regime_key = str(ctx.get("regimeBucket", "?"))
        regime_buckets.setdefault(regime_key, []).append(rec)

        # VRP score bucket
        vrp_score = ctx.get("vrpScore")
        if vrp_score is not None:
            vs = float(vrp_score)
            if vs >= 75:
                vk = "75+"
            elif vs >= 60:
                vk = "60-75"
            elif vs >= 40:
                vk = "40-60"
            else:
                vk = "<40"
        else:
            vk = "?"
        vrp_buckets.setdefault(vk, []).append(rec)

        # Breach rate bucket
        breach_pct = ctx.get("breachPct")
        if breach_pct is not None:
            bp = float(breach_pct)
            if bp < 10:
                bk = "<10%"
            elif bp < 20:
                bk = "10-20%"
            elif bp < 30:
                bk = "20-30%"
            else:
                bk = "30%+"
        else:
            bk = "?"
        breach_buckets.setdefault(bk, []).append(rec)

        # AMC/BMO timing bucket
        timing_key = str(ctx.get("earningsTiming", "?")).upper()
        timing_buckets.setdefault(timing_key, []).append(rec)

        # Verdict calibration
        verdict = advisor.get("verdict", "?")
        if verdict not in verdict_outcomes:
            verdict_outcomes[verdict] = {"win": 0, "loss": 0, "scratch": 0, "total": 0}
        verdict_outcomes[verdict]["total"] += 1
        if oc in ("win", "loss", "scratch"):
            verdict_outcomes[verdict][oc] += 1

    total = len(closed)
    total_decided = wins + losses + scratches
    win_rate = round(wins / total_decided * 100, 1) if total_decided > 0 else None
    avg_pnl = round(statistics.mean(pnl_list), 2) if pnl_list else None
    total_pnl = round(sum(pnl_list), 2) if pnl_list else None
    median_pnl = round(statistics.median(pnl_list), 2) if pnl_list else None

    avg_win = None
    avg_loss = None
    if pnl_list:
        win_pnls = [p for p in pnl_list if p > 0]
        loss_pnls = [p for p in pnl_list if p < 0]
        avg_win = round(statistics.mean(win_pnls), 2) if win_pnls else None
        avg_loss = round(statistics.mean(loss_pnls), 2) if loss_pnls else None

    risk_tendency = "balanced"
    if win_rate is not None:
        if win_rate > 85 and avg_pnl is not None and avg_pnl < 0.5:
            risk_tendency = "too_conservative"
        elif win_rate < 40:
            risk_tendency = "too_aggressive"
        elif avg_loss is not None and avg_win is not None and abs(avg_loss) > avg_win * 3:
            risk_tendency = "risk_reward_skewed"

    def _bucket_summary(bucket: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k, recs in sorted(bucket.items()):
            ws = sum(1 for r in recs if r["outcomeClass"] == "win")
            ls = sum(1 for r in recs if r["outcomeClass"] == "loss")
            ps = [r["pnl"] for r in recs if r["pnl"] is not None]
            n = ws + ls + sum(1 for r in recs if r["outcomeClass"] == "scratch")
            out[k] = {
                "n": len(recs),
                "winRate": round(ws / n * 100, 1) if n > 0 else None,
                "avgPnl": round(statistics.mean(ps), 2) if ps else None,
            }
        return out

    # --- v2 enrichments ---

    # Recent trades with post-mortem lessons (last 10)
    recent_trades: List[Dict[str, Any]] = []
    for t in closed[:10]:
        rt: Dict[str, Any] = {
            "tradeId": t.get("tradeId"),
            "ticker": t.get("ticker"),
            "earningsTiming": (t.get("entryContext") or {}).get("earningsTiming"),
            "emMultiple": (t.get("entry") or {}).get("emMultiple"),
            "wingWidth": (t.get("entry") or {}).get("wingWidth"),
            "outcome": (t.get("outcome") or {}).get("outcomeClass"),
            "pnl": (t.get("outcome") or {}).get("realizedPnl"),
            "closedAt": t.get("closedAt"),
            "actualMove": (t.get("outcome") or {}).get("actualMove"),
            "moveVsPredicted": (t.get("outcome") or {}).get("moveVsPredicted"),
            "breachOccurred": (t.get("outcome") or {}).get("breachOccurred"),
            "holdDecision": (t.get("outcome") or {}).get("holdDecision"),
            "tags": ((t.get("outcome") or {}).get("autoTags") or [])
                   + ((t.get("outcome") or {}).get("userTags") or []),
        }
        pm = t.get("postMortem")
        if pm:
            rt["lesson"] = pm.get("lesson")
            rt["category"] = pm.get("category")
        recent_trades.append(rt)

    # Pattern insights
    pattern_insights: List[str] = []
    try:
        from backend.trade_memory import detect_patterns
        pattern_insights = detect_patterns(closed, engine="e1")
    except Exception:
        pass

    # Tag analysis
    tag_analysis: Dict[str, Dict[str, Any]] = {}
    for t in closed:
        outcome = t.get("outcome") or {}
        oc = outcome.get("outcomeClass")
        pnl_val = outcome.get("realizedPnl")
        all_tags = (outcome.get("autoTags") or []) + (outcome.get("userTags") or [])
        for tag in all_tags:
            if tag not in tag_analysis:
                tag_analysis[tag] = {"count": 0, "wins": 0, "pnls": []}
            tag_analysis[tag]["count"] += 1
            if oc == "win":
                tag_analysis[tag]["wins"] += 1
            if pnl_val is not None:
                tag_analysis[tag]["pnls"].append(float(pnl_val))
    tag_summary: Dict[str, Any] = {}
    for tag, data in tag_analysis.items():
        n = data["count"]
        tag_summary[tag] = {
            "n": n,
            "winRate": round(data["wins"] / n * 100, 1) if n > 0 else None,
            "avgPnl": round(statistics.mean(data["pnls"]), 2) if data["pnls"] else None,
        }

    # Weekly trend
    weekly_trend = None
    if len(pnl_list) >= 5:
        last5_pnl = pnl_list[-5:]
        last5_outcomes = [
            (t.get("outcome") or {}).get("outcomeClass")
            for t in closed[:5]
        ]
        l5_wins = sum(1 for o in last5_outcomes if o == "win")
        weekly_trend = {
            "rolling5WinRate": round(l5_wins / 5 * 100, 1),
            "rolling5AvgPnl": round(statistics.mean(last5_pnl), 2),
            "improving": l5_wins >= 3 and (round(statistics.mean(last5_pnl), 2) > (avg_pnl or 0)),
        }

    # Streak info
    streak_info = None
    recent_ocs = [
        (t.get("outcome") or {}).get("outcomeClass")
        for t in closed if (t.get("outcome") or {}).get("outcomeClass") in ("win", "loss")
    ]
    if recent_ocs:
        streak = 1
        for i in range(1, len(recent_ocs)):
            if recent_ocs[i] == recent_ocs[0]:
                streak += 1
            else:
                break
        streak_info = {
            "currentType": recent_ocs[0],
            "currentStreak": streak,
            "tiltWarning": streak >= 3 and recent_ocs[0] == "loss",
        }

    # VRP calibration: predicted vs actual move ratio across all closed trades
    vrp_calibration: Dict[str, Any] = {"hasData": False}
    move_ratios: List[float] = []
    em_breach_actual: Dict[str, Dict[str, int]] = {}
    for t in closed:
        outcome = t.get("outcome") or {}
        mvp = outcome.get("moveVsPredicted")
        if mvp is not None:
            move_ratios.append(float(mvp))
        em_key = str(t.get("entry", {}).get("emMultiple", "?"))
        if em_key not in em_breach_actual:
            em_breach_actual[em_key] = {"total": 0, "breached": 0}
        em_breach_actual[em_key]["total"] += 1
        if outcome.get("breachOccurred"):
            em_breach_actual[em_key]["breached"] += 1

    if move_ratios:
        vrp_calibration = {
            "hasData": True,
            "avgMoveRatio": round(statistics.mean(move_ratios), 3),
            "medianMoveRatio": round(statistics.median(move_ratios), 3),
            "sampleSize": len(move_ratios),
            "volCrushRate": round(sum(1 for r in move_ratios if r < 1.0) / len(move_ratios) * 100, 1),
            "strongCrushRate": round(sum(1 for r in move_ratios if r < 0.75) / len(move_ratios) * 100, 1),
        }

    breach_calibration: Dict[str, Any] = {}
    for em_key, counts in em_breach_actual.items():
        if counts["total"] >= 2:
            breach_calibration[em_key] = {
                "total": counts["total"],
                "breached": counts["breached"],
                "actualBreachRate": round(counts["breached"] / counts["total"] * 100, 1),
            }

    return {
        "totalClosed": total,
        "hasData": True,
        "wins": wins,
        "losses": losses,
        "scratches": scratches,
        "winRate": win_rate,
        "avgPnl": avg_pnl,
        "medianPnl": median_pnl,
        "totalPnl": total_pnl,
        "avgWin": avg_win,
        "avgLoss": avg_loss,
        "riskTendency": risk_tendency,
        "byEm": _bucket_summary(em_buckets),
        "byWing": _bucket_summary(wing_buckets),
        "byRegime": _bucket_summary(regime_buckets),
        "byVrpBucket": _bucket_summary(vrp_buckets),
        "byBreachBucket": _bucket_summary(breach_buckets),
        "byTiming": _bucket_summary(timing_buckets),
        "verdictCalibration": verdict_outcomes,
        "recentTrades": recent_trades,
        "patternInsights": pattern_insights,
        "tagAnalysis": tag_summary,
        "weeklyTrend": weekly_trend,
        "streakInfo": streak_info,
        "vrpCalibration": vrp_calibration,
        "breachCalibrationByEm": breach_calibration,
    }


# ---------------------------------------------------------------------------
# Engine 15 digest — compact, separate from E1's cross-ticker buckets.
# ---------------------------------------------------------------------------

def compute_e15_trade_performance_digest(
    store: Optional[RedisStore] = None,
) -> Dict[str, Any]:
    """Aggregate closed Engine 15 (earnings-IC scenario) trades.

    Engine 15 trades carry ``source="engine15"`` and a different entry
    schema: strikes on the wings, planned-exit date + hours, and an
    attached scenario snapshot under ``entryContext.engine15Scenario``.
    We compute a compact digest the E15 advisor / UI can read to show
    the user how their sim calibrates against actual outcomes across
    names.
    """
    closed = [
        t for t in list_closed_trades(store=store, limit=400)
        if str(t.get("source") or "").lower() == "engine15"
    ]
    if not closed:
        return {"totalClosed": 0, "hasData": False}

    pnl_list: List[float] = []
    wins = losses = scratches = 0
    by_timing: Dict[str, List[float]] = {}
    sim_error_pp: List[float] = []
    by_ticker: Dict[str, List[float]] = {}

    for t in closed:
        outcome = t.get("outcome") or {}
        oc = outcome.get("outcomeClass")
        if oc == "win":
            wins += 1
        elif oc == "loss":
            losses += 1
        elif oc == "scratch":
            scratches += 1
        pnl = outcome.get("realizedPnl")
        if pnl is not None:
            pnl_list.append(float(pnl))
        entry = t.get("entry") or {}
        timing = str(entry.get("earningsTiming") or "UNK").upper()
        if pnl is not None:
            by_timing.setdefault(timing, []).append(float(pnl))
            by_ticker.setdefault(str(t.get("ticker") or "?"), []).append(float(pnl))
        # Calibration: compare actual realized P&L % to sim's meanPnlPct if available.
        ctx = t.get("entryContext") or {}
        scenario = ctx.get("engine15Scenario") or {}
        ev = scenario.get("expectedValue") or {}
        sim_mean = ev.get("meanPnlPct")
        actual = outcome.get("pnlPct")
        if (sim_mean is not None) and (actual is not None):
            try:
                sim_error_pp.append(float(actual) - float(sim_mean))
            except (TypeError, ValueError):
                pass

    def _stats(xs: List[float]) -> Dict[str, Any]:
        if not xs:
            return {"n": 0}
        return {
            "n": len(xs),
            "mean": round(statistics.mean(xs), 3),
            "median": round(statistics.median(xs), 3),
        }

    return {
        "totalClosed": len(closed),
        "hasData": True,
        "wins": wins, "losses": losses, "scratches": scratches,
        "winRatePct": round((wins / len(closed)) * 100.0, 1) if closed else 0.0,
        "avgPnl": _stats(pnl_list).get("mean"),
        "medianPnl": _stats(pnl_list).get("median"),
        "byTiming": {k: _stats(v) for k, v in by_timing.items()},
        "byTicker": {k: _stats(v) for k, v in by_ticker.items() if len(v) >= 2},
        "simErrorPP": _stats(sim_error_pp),  # (actual - predicted) in pp, mean/median/n
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _list_all(store: Optional[RedisStore] = None) -> List[Dict[str, Any]]:
    s = store or get_store_optional()
    if s is None:
        return []
    try:
        raw = s.get_json(_INDEX_KEY)
        if not isinstance(raw, list):
            return []
        out: List[Dict[str, Any]] = []
        for tid in raw:
            t = s.get_json(_trade_key(str(tid)))
            if isinstance(t, dict):
                out.append(t)
        return out
    except Exception:
        return []


def _append_index(
    store: RedisStore,
    trade_id: str,
    max_len: int = 200,
    ttl: int = 60 * 86400,
) -> None:
    idx = store.get_json(_INDEX_KEY)
    if not isinstance(idx, list):
        idx = []
    idx.append(trade_id)
    if len(idx) > max_len:
        idx = idx[-max_len:]
    store.set_json(_INDEX_KEY, idx, ttl_s=ttl)
