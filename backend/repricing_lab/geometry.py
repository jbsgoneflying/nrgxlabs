"""Entry geometry + structural invalidation (stop menu)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class EntryPlan:
    entry_type: str          # next_open | breakout | pullback
    entry_price: float
    session: str


@dataclass(frozen=True)
class StopPlan:
    stop_type: str           # swing_low | atr_multiple | event_gap_fill
    stop_price: float
    risk_per_share: float


def atr_stop(entry: float, atr: float, *, multiple: float = 1.5, side: str = "long") -> StopPlan:
    if atr <= 0:
        raise ValueError("atr must be positive")
    if side == "long":
        stop = entry - multiple * atr
    else:
        stop = entry + multiple * atr
    return StopPlan(
        stop_type="atr_multiple",
        stop_price=round(stop, 6),
        risk_per_share=round(abs(entry - stop), 6),
    )


def swing_stop(
    entry: float,
    bars: Sequence[Dict[str, Any]],
    *,
    lookback: int = 10,
    side: str = "long",
    buffer: float = 0.0,
) -> Optional[StopPlan]:
    if len(bars) < lookback:
        return None
    window = bars[-lookback:]
    if side == "long":
        lows = [float(b["low"]) for b in window if b.get("low") is not None]
        if not lows:
            return None
        stop = min(lows) - buffer
    else:
        highs = [float(b["high"]) for b in window if b.get("high") is not None]
        if not highs:
            return None
        stop = max(highs) + buffer
    return StopPlan(
        stop_type="swing_low" if side == "long" else "swing_high",
        stop_price=round(stop, 6),
        risk_per_share=round(abs(entry - stop), 6),
    )


def next_open_entry(bars: Sequence[Dict[str, Any]], *, decision_session: str) -> Optional[EntryPlan]:
    """First bar strictly after decision_session → open entry."""
    for b in bars:
        sess = str(b.get("session_date") or "")[:10]
        if sess > decision_session[:10] and b.get("open") is not None:
            return EntryPlan(entry_type="next_open", entry_price=float(b["open"]), session=sess)
    return None


def size_shares(
    *,
    account_size: float,
    risk_pct: float,
    risk_per_share: float,
    gap_stress: float,
    slippage: float,
    adv20_usd: Optional[float],
    price: float,
    adv_participation_pct: float = 2.0,
) -> Dict[str, float]:
    """Gap-stress-aware sizing per plan §9."""
    if risk_per_share <= 0 or price <= 0:
        return {"shares": 0.0, "risk_dollars": 0.0, "notional": 0.0}
    rps = max(risk_per_share, gap_stress) + max(0.0, slippage)
    budget = account_size * (risk_pct / 100.0)
    shares = budget / rps
    notional = shares * price
    if adv20_usd and adv20_usd > 0:
        cap_notional = adv20_usd * (adv_participation_pct / 100.0)
        if notional > cap_notional:
            shares = cap_notional / price
            notional = shares * price
    return {
        "shares": float(int(shares)),  # whole shares
        "risk_dollars": float(int(shares)) * rps,
        "notional": float(int(shares)) * price,
        "risk_per_share_used": rps,
    }


def plans_to_json(entry: EntryPlan, stop: StopPlan) -> Dict[str, Any]:
    return {"entry": asdict(entry), "stop": asdict(stop)}
