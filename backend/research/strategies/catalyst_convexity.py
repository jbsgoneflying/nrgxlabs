"""Strategy #2 (Tier 2 pilot) — Catalyst convexity / "lotto" sleeve.

Thesis: around scheduled binary catalysts (FDA PDUFA, key product events,
court/regulatory rulings) the implied move can underprice the bimodal outcome.
Instead of *selling* premium (the rest of the NRGX book), we *buy* defined-risk
convexity — here an ATM straddle — entered ~2 weeks before the event and exited
just after it. P&L is the return on premium paid (a different basis from the
equity strategies — noted in the report).

Status: PILOT. Two hard realities the plan flagged:
  * **No historical catalyst calendar exists** — it must be hand-curated (seed at
    ``data/universe/catalyst_calendar.json``, pattern of geopolitical_shocks.json).
  * **Options coverage / liquidity** on small-cap biotech is shaky and samples are
    lumpy/small-n -> low statistical power. Treat results as directional only.

The study is decoupled from ORATS via ``ChainProvider`` so it is unit-testable
offline with in-memory chains; the live ORATS adapter lives in ``live_providers``.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Sequence

from backend.research.cost_model import CostModel
from backend.research.data_provider import (
    ChainProvider,
    OptionQuote,
    PriceProvider,
    PriceSeries,
)
from backend.research.event_study import EventStudyOutcome, TradeResult

_LOG = logging.getLogger(__name__)

_CALENDAR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "data", "universe", "catalyst_calendar.json",
)


@dataclass(frozen=True)
class Catalyst:
    ticker: str
    event_date: str
    kind: str = "catalyst"   # e.g. "fda_pdufa", "product", "ruling"
    note: str = ""


def load_catalyst_calendar(path: Optional[str] = None) -> List[Catalyst]:
    path = path or _CALENDAR_PATH
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        raw = json.load(fh)
    events = raw.get("events", raw) if isinstance(raw, dict) else raw
    out: List[Catalyst] = []
    for e in events or []:
        t = str(e.get("ticker") or "").upper()
        d = str(e.get("event_date") or e.get("date") or "")[:10]
        if t and d:
            out.append(Catalyst(t, d, str(e.get("kind") or "catalyst"), str(e.get("note") or "")))
    return out


def _select_atm_straddle(
    chain: Sequence[OptionQuote],
    spot: float,
    *,
    min_dte: int,
    target_dte: int,
) -> Optional[tuple]:
    """Pick (call, put) at the near-ATM strike of the best expiry.

    Best expiry = the one whose dte is >= ``min_dte`` and closest to ``target_dte``.
    Returns (call, put) OptionQuotes or None if no usable pair exists.
    """
    eligible = [q for q in chain if q.dte >= min_dte]
    if not eligible:
        return None
    # choose expiry by dte closeness to target among eligible
    best_dte = min({q.dte for q in eligible}, key=lambda d: abs(d - target_dte))
    leg = [q for q in chain if q.dte == best_dte]
    strikes = sorted({q.strike for q in leg})
    if not strikes:
        return None
    atm = min(strikes, key=lambda s: abs(s - spot))
    call = next((q for q in leg if q.strike == atm and q.right.upper() == "C"), None)
    put = next((q for q in leg if q.strike == atm and q.right.upper() == "P"), None)
    if call is None or put is None or call.mid <= 0 or put.mid <= 0:
        return None
    return call, put


def run_convexity_study(
    catalysts: Sequence[Catalyst],
    price_provider: PriceProvider,
    chain_provider: ChainProvider,
    *,
    entry_lead_days: int = 10,
    exit_offset_days: int = 1,
    target_dte: int = 30,
    min_remaining_dte: int = 7,
    premium_cost_pct: float = 0.04,
    history_buffer_days: int = 30,
) -> EventStudyOutcome:
    """Backtest a long ATM straddle into each catalyst; P&L = return on premium.

    entry_lead_days: trading days before the event to buy the straddle.
    exit_offset_days: trading days after the event to close.
    premium_cost_pct: round-trip cost as a fraction of premium (wide option spreads).
    """
    outcome = EventStudyOutcome()
    for cat in catalysts:
        lo = _shift(cat.event_date, -history_buffer_days)
        hi = _shift(cat.event_date, history_buffer_days)
        try:
            bars = price_provider.get_bars(cat.ticker, lo, hi)
        except Exception:
            outcome.skipped.append({"ticker": cat.ticker, "signal_date": cat.event_date, "reason": "price_fetch_error"})
            continue
        series = PriceSeries(bars)
        ev_idx = series.index_on_or_after(cat.event_date)
        if ev_idx is None:
            outcome.skipped.append({"ticker": cat.ticker, "signal_date": cat.event_date, "reason": "no_event_bar"})
            continue
        entry_idx = ev_idx - entry_lead_days
        exit_idx = ev_idx + exit_offset_days
        entry_bar = series.bar_at(entry_idx)
        exit_bar = series.bar_at(exit_idx)
        if entry_bar is None or exit_bar is None:
            outcome.skipped.append({"ticker": cat.ticker, "signal_date": cat.event_date, "reason": "insufficient_bars"})
            continue

        entry_chain = chain_provider.get_chain(cat.ticker, entry_bar.date)
        min_dte = entry_lead_days + exit_offset_days + min_remaining_dte
        pair = _select_atm_straddle(entry_chain, entry_bar.close, min_dte=min_dte, target_dte=target_dte)
        if pair is None:
            outcome.skipped.append({"ticker": cat.ticker, "signal_date": cat.event_date, "reason": "no_entry_straddle"})
            continue
        call, put = pair
        entry_premium = call.mid + put.mid

        # Reprice the SAME strike+expiry at exit.
        exit_chain = chain_provider.get_chain(cat.ticker, exit_bar.date)
        ex_call = next((q for q in exit_chain if q.expiry == call.expiry and q.strike == call.strike and q.right.upper() == "C"), None)
        ex_put = next((q for q in exit_chain if q.expiry == put.expiry and q.strike == put.strike and q.right.upper() == "P"), None)
        if ex_call is None or ex_put is None:
            outcome.skipped.append({"ticker": cat.ticker, "signal_date": cat.event_date, "reason": "no_exit_straddle"})
            continue
        exit_premium = ex_call.mid + ex_put.mid

        gross = (exit_premium - entry_premium) / entry_premium
        cost = premium_cost_pct
        net = gross - cost
        outcome.results.append(
            TradeResult(
                ticker=cat.ticker,
                strategy="CatalystConvexity",
                signal_date=entry_bar.date,
                entry_date=entry_bar.date,
                exit_date=exit_bar.date,
                direction=1,
                entry_price=round(entry_premium, 4),
                exit_price=round(exit_premium, 4),
                gross_return=round(gross, 6),
                cost=round(cost, 6),
                net_return=round(net, 6),
                holding_days=exit_idx - entry_idx,
                tags={"kind": cat.kind},
            )
        )

    outcome.results.sort(key=lambda r: (r.exit_date, r.ticker))
    return outcome


def _shift(date: str, days: int) -> str:
    import datetime as dt

    return (dt.date.fromisoformat(date[:10]) + dt.timedelta(days=int(days))).isoformat()
