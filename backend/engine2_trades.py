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

    trade["outcome"] = {
        "entryCredit": entry_credit,
        "exitCredit": float(exit_credit) if exit_credit is not None else None,
        "realizedPnl": float(realized_pnl) if realized_pnl is not None else None,
        "outcomeClass": outcome_class,
        "notes": cd.get("notes"),
        "expiredWorthless": bool(cd.get("expiredWorthless", False)),
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
    }


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
