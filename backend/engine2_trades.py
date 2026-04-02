"""Engine 2 — Trade Persistence (Redis).

Redis key layout (mirrors Engine 12 pattern):
    e2:trades:{trade_id}    — individual trade JSON document
    e2:trades:index         — ordered list of trade IDs (newest last)

Supports both recommended (advisor-sourced) and user-adjusted trades.
Closed trades carry structured outcome data used by the performance digest
to feed learning context back into the LLM advisory loop.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import statistics
import uuid
from typing import Any, Dict, List, Optional

from backend.config import FeatureFlags, get_flags
from backend.redis_store import RedisStore, get_store_optional

LOG = logging.getLogger(__name__)

_TRADE_KEY_PREFIX = "e2:trades:"
_TRADE_INDEX_KEY = "e2:trades:index"


def _trade_ttl(flags: Optional[FeatureFlags] = None) -> int:
    f = flags or get_flags()
    return int(f.ENGINE2_TRADE_TTL_S)


def _trade_max_index(flags: Optional[FeatureFlags] = None) -> int:
    f = flags or get_flags()
    return int(f.ENGINE2_TRADE_MAX_INDEX)


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _generate_trade_id(underlying: str = "SPX") -> str:
    today = dt.date.today().strftime("%Y%m%d")
    short_uuid = uuid.uuid4().hex[:8]
    return f"e2-{today}-{underlying}-{short_uuid}"


def log_trade(
    trade_data: Dict[str, Any],
    store: Optional[RedisStore] = None,
    flags: Optional[FeatureFlags] = None,
) -> Optional[str]:
    """Persist a new trade to Redis. Returns trade_id or None on failure."""
    s = store or get_store_optional()
    if s is None:
        LOG.warning("engine2_trades.log_trade: Redis unavailable")
        return None

    underlying = str(trade_data.get("entry", {}).get("underlying", "SPX"))
    trade_id = _generate_trade_id(underlying)
    ttl = _trade_ttl(flags)
    max_idx = _trade_max_index(flags)

    source = trade_data.get("source", "advisor")
    trade = {
        "tradeId": trade_id,
        "status": "active",
        "loggedAt": _utcnow_iso(),
        "source": source,
        "entry": trade_data.get("entry", {}),
        "entryContext": trade_data.get("entryContext", {}),
        "marketSnapshot": trade_data.get("marketSnapshot", {}),
        "positionGreeks": trade_data.get("positionGreeks", {}),
        "advisorVerdict": trade_data.get("advisorVerdict"),
        "originalTicket": trade_data.get("originalTicket") if source == "adjusted" else None,
        "adjustmentNote": trade_data.get("adjustmentNote") if source == "adjusted" else None,
        "checkIns": [],
        "closedAt": None,
        "closeReason": None,
        "outcome": None,
    }

    if not s.set_json(f"{_TRADE_KEY_PREFIX}{trade_id}", trade, ttl_s=ttl):
        LOG.error("engine2_trades.log_trade: failed to write trade %s", trade_id)
        return None

    index: list = s.get_json(_TRADE_INDEX_KEY) or []
    index.append(trade_id)
    index = index[-max_idx:]
    s.set_json(_TRADE_INDEX_KEY, index, ttl_s=ttl)

    LOG.info("engine2_trades: logged trade %s", trade_id)
    return trade_id


def _load_trade(trade_id: str, store: RedisStore) -> Optional[Dict[str, Any]]:
    return store.get_json(f"{_TRADE_KEY_PREFIX}{trade_id}")


def list_active_trades(
    store: Optional[RedisStore] = None,
) -> List[Dict[str, Any]]:
    """Return all active trades (not closed)."""
    s = store or get_store_optional()
    if s is None:
        return []

    index: list = s.get_json(_TRADE_INDEX_KEY) or []
    trades: List[Dict[str, Any]] = []
    for tid in index:
        t = _load_trade(str(tid), s)
        if t and t.get("status") in ("active", "monitoring"):
            trades.append(t)
    return trades


def get_trade(
    trade_id: str,
    store: Optional[RedisStore] = None,
) -> Optional[Dict[str, Any]]:
    """Load a single trade by ID."""
    s = store or get_store_optional()
    if s is None:
        return None
    return _load_trade(trade_id, s)


def close_trade(
    trade_id: str,
    close_data: Optional[Dict[str, Any]] = None,
    store: Optional[RedisStore] = None,
    flags: Optional[FeatureFlags] = None,
) -> Optional[Dict[str, Any]]:
    """Close an active trade. Returns updated trade or None."""
    s = store or get_store_optional()
    if s is None:
        return None

    trade = _load_trade(trade_id, s)
    if trade is None:
        return None

    cd = close_data or {}
    trade["status"] = "closed"
    trade["closedAt"] = _utcnow_iso()
    trade["closeReason"] = cd.get("reason", "manual")

    entry_credit = float(trade.get("entry", {}).get("entryCredit", 0))
    exit_credit = cd.get("exitCredit")
    realized_pnl = cd.get("realizedPnl")
    if exit_credit is not None and realized_pnl is None:
        exit_credit = float(exit_credit)
        realized_pnl = round(entry_credit - exit_credit, 2)

    outcome_class = cd.get("outcomeClass")
    if outcome_class is None and realized_pnl is not None:
        if realized_pnl > 0:
            outcome_class = "win"
        elif realized_pnl < -0.01:
            outcome_class = "loss"
        else:
            outcome_class = "scratch"

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

    # Max drawdown from check-ins (highest breach proximity seen)
    max_breach_prox = 0.0
    peak_profit = None
    for ci in trade.get("checkIns", []):
        tracking = ci.get("tracking", {}) or {}
        bp_put = float(tracking.get("breachProxPut", 0) or tracking.get("breachProximityPut", 0) or 0)
        bp_call = float(tracking.get("breachProxCall", 0) or tracking.get("breachProximityCall", 0) or 0)
        max_breach_prox = max(max_breach_prox, bp_put, bp_call)

    # DTE at exit
    dte_at_exit = None
    expiry = trade.get("entry", {}).get("expiryDate")
    if expiry:
        try:
            dte_at_exit = max((dt.date.fromisoformat(expiry) - dt.date.today()).days, 0)
        except Exception:
            pass

    # Auto outcome tags
    auto_tags: list = []
    try:
        from backend.trade_memory import compute_auto_tags_e2
        auto_tags = compute_auto_tags_e2(trade)
    except Exception:
        pass
    user_tags = cd.get("outcomeTags", [])

    trade["outcome"] = {
        "entryCredit": entry_credit,
        "exitCredit": float(exit_credit) if exit_credit is not None else None,
        "realizedPnl": float(realized_pnl) if realized_pnl is not None else None,
        "outcomeClass": outcome_class,
        "notes": cd.get("notes"),
        "expiredWorthless": bool(cd.get("expiredWorthless", False)),
        "spotAtExit": cd.get("spotAtExit"),
        "vixAtExit": cd.get("vixAtExit"),
        "regimeAtExit": cd.get("regimeAtExit"),
        "dteAtExit": dte_at_exit,
        "holdDurationDays": hold_duration_days,
        "maxBreachProximity": round(max_breach_prox, 1),
        "autoTags": auto_tags,
        "userTags": user_tags if isinstance(user_tags, list) else [],
    }

    ttl = _trade_ttl(flags)
    s.set_json(f"{_TRADE_KEY_PREFIX}{trade_id}", trade, ttl_s=ttl)
    LOG.info("engine2_trades: closed trade %s reason=%s outcome=%s", trade_id, trade["closeReason"], outcome_class)
    return trade


def list_closed_trades(
    store: Optional[RedisStore] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return closed trades, newest first (up to limit)."""
    s = store or get_store_optional()
    if s is None:
        return []
    index: list = s.get_json(_TRADE_INDEX_KEY) or []
    trades: List[Dict[str, Any]] = []
    for tid in reversed(index):
        t = _load_trade(str(tid), s)
        if t and t.get("status") == "closed":
            trades.append(t)
            if len(trades) >= limit:
                break
    return trades


def list_all_trades(
    store: Optional[RedisStore] = None,
) -> List[Dict[str, Any]]:
    """Return every trade in the index regardless of status."""
    s = store or get_store_optional()
    if s is None:
        return []
    index: list = s.get_json(_TRADE_INDEX_KEY) or []
    trades: List[Dict[str, Any]] = []
    for tid in index:
        t = _load_trade(str(tid), s)
        if t:
            trades.append(t)
    return trades


def compute_trade_performance_digest(
    store: Optional[RedisStore] = None,
) -> Dict[str, Any]:
    """Aggregate closed-trade performance into a learning digest.

    Computes win rate, average P&L, calibration metrics, and breakdowns
    by EM multiple, wing width, and regime — all fed back into the LLM
    prompt as institutional memory.
    """
    closed = list_closed_trades(store=store, limit=100)
    if not closed:
        return {"totalClosed": 0, "hasData": False}

    wins, losses, scratches = 0, 0, 0
    pnl_list: List[float] = []
    em_buckets: Dict[str, List[Dict[str, Any]]] = {}
    wing_buckets: Dict[str, List[Dict[str, Any]]] = {}
    regime_buckets: Dict[str, List[Dict[str, Any]]] = {}
    verdict_outcomes: Dict[str, Dict[str, int]] = {}
    adjusted_count = 0

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

        if t.get("source") == "adjusted":
            adjusted_count += 1

        rec = {"outcomeClass": oc, "pnl": pnl}
        em_key = str(entry.get("emMultiple", "?"))
        em_buckets.setdefault(em_key, []).append(rec)
        wing_key = f"${entry.get('wingWidth', '?')}"
        wing_buckets.setdefault(wing_key, []).append(rec)
        regime_key = str(ctx.get("regimeBucket", "?"))
        regime_buckets.setdefault(regime_key, []).append(rec)

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

    def _bucket_summary(bucket: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
        out = {}
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

    # --- v2 enrichments ---

    # Recent trades with post-mortem lessons (last 10, not just aggregates)
    recent_trades: List[Dict[str, Any]] = []
    for t in closed[:10]:
        rt: Dict[str, Any] = {
            "tradeId": t.get("tradeId"),
            "entryDate": t.get("entry", {}).get("entryDate") or (t.get("loggedAt") or "")[:10],
            "closedAt": t.get("closedAt"),
            "emMultiple": t.get("entry", {}).get("emMultiple"),
            "wingWidth": t.get("entry", {}).get("wingWidth"),
            "outcome": (t.get("outcome") or {}).get("outcomeClass"),
            "pnl": (t.get("outcome") or {}).get("realizedPnl"),
            "holdDuration": (t.get("outcome") or {}).get("holdDurationDays"),
            "maxBreachProx": (t.get("outcome") or {}).get("maxBreachProximity"),
            "tags": ((t.get("outcome") or {}).get("autoTags") or [])
                   + ((t.get("outcome") or {}).get("userTags") or []),
        }
        pm = t.get("postMortem")
        if pm:
            rt["lesson"] = pm.get("lesson")
            rt["category"] = pm.get("category")
        recent_trades.append(rt)

    # Pattern insights from the corpus
    pattern_insights: List[str] = []
    try:
        from backend.trade_memory import detect_patterns
        pattern_insights = detect_patterns(closed, engine="e2")
    except Exception:
        pass

    # Tag analysis: win rate and avg P&L by outcome tag
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

    # Weekly trend: rolling 5-trade metrics
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
        "adjustedCount": adjusted_count,
        "riskTendency": risk_tendency,
        "byEm": _bucket_summary(em_buckets),
        "byWing": _bucket_summary(wing_buckets),
        "byRegime": _bucket_summary(regime_buckets),
        "verdictCalibration": verdict_outcomes,
        "recentTrades": recent_trades,
        "patternInsights": pattern_insights,
        "tagAnalysis": tag_summary,
        "weeklyTrend": weekly_trend,
        "streakInfo": streak_info,
    }


def set_post_mortem(
    trade_id: str,
    post_mortem: Dict[str, Any],
    store: Optional[RedisStore] = None,
    flags: Optional[FeatureFlags] = None,
) -> Optional[Dict[str, Any]]:
    """Attach a post-mortem to a closed trade. Returns updated trade or None."""
    s = store or get_store_optional()
    if s is None:
        return None

    trade = _load_trade(trade_id, s)
    if trade is None or trade.get("status") != "closed":
        return None

    trade["postMortem"] = post_mortem
    ttl = _trade_ttl(flags)
    s.set_json(f"{_TRADE_KEY_PREFIX}{trade_id}", trade, ttl_s=ttl)
    LOG.info("engine2_trades: post-mortem set for %s category=%s", trade_id, post_mortem.get("category"))
    return trade


def add_checkin(
    trade_id: str,
    checkin_data: Dict[str, Any],
    store: Optional[RedisStore] = None,
    flags: Optional[FeatureFlags] = None,
) -> Optional[Dict[str, Any]]:
    """Append a check-in record to a trade. Returns updated trade or None."""
    s = store or get_store_optional()
    if s is None:
        return None

    trade = _load_trade(trade_id, s)
    if trade is None:
        return None

    checkin = {
        "timestamp": _utcnow_iso(),
        **checkin_data,
    }

    checkins = trade.get("checkIns") or []
    checkins.append(checkin)
    trade["checkIns"] = checkins[-20:]

    if checkin_data.get("status") in ("adjust", "exit"):
        trade["status"] = "monitoring"

    ttl = _trade_ttl(flags)
    s.set_json(f"{_TRADE_KEY_PREFIX}{trade_id}", trade, ttl_s=ttl)
    LOG.info("engine2_trades: check-in for %s status=%s", trade_id, checkin_data.get("status"))
    return trade
