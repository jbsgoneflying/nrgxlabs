"""Trade-idea generation for the AI Capex Reality Engine.

Turns deterministic ``TickerVerdict`` labels into desk-ready expressions:

- **Directional** single-name (long the real/pre-consensus winners, short the
  overhyped / second-order losers).
- **Baskets** per category when multiple names agree on direction.
- **Option structures** (debit/credit verticals) selected by direction and,
  when an ORATS client is supplied, the name's IV rank — buy premium when vol
  is cheap, sell premium when it's rich.

All of this is downstream of the labels (which are themselves deterministic),
so nothing here introduces LLM-driven sizing. Option structures are
*suggestions*; the desk's IC engines (E1/E2/E14/E15) size and validate fills.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.ai_capex import models
from backend.ai_capex.models import TickerVerdict

LOG = logging.getLogger("ai_capex.trades")


def _iv_rank(ticker: str, orats_client: Any) -> Optional[float]:
    """Best-effort IV rank (0..100) from ORATS cores; None if unavailable."""
    if orats_client is None:
        return None
    try:
        resp = orats_client.cores(ticker=str(ticker).upper(), fields="ticker,ivRank,iv30d")
        rows = getattr(resp, "rows", None) or []
        if not rows:
            return None
        row = rows[0]
        rank = row.get("ivRank") if isinstance(row, dict) else None
        if rank is None:
            return None
        rank = float(rank)
        return rank * 100.0 if rank <= 1.0 else rank
    except Exception:  # pragma: no cover - defensive
        return None


def _option_structure(direction: str, iv_rank: Optional[float]) -> Dict[str, Any]:
    """Pick a vertical structure from direction + (optional) IV rank."""
    rich = iv_rank is not None and iv_rank >= 60.0
    cheap = iv_rank is not None and iv_rank <= 35.0
    iv_note = (f"IV rank {iv_rank:.0f}" if iv_rank is not None else "IV rank n/a")

    if direction == "long":
        if rich:
            return {"structure": "short put spread", "premium": "sell", "thesis": "bullish, sell rich vol", "ivNote": iv_note}
        if cheap:
            return {"structure": "call debit spread", "premium": "buy", "thesis": "bullish, buy cheap convexity", "ivNote": iv_note}
        return {"structure": "call debit spread", "premium": "buy", "thesis": "bullish directional", "ivNote": iv_note}
    if direction == "short":
        if rich:
            return {"structure": "bear call spread", "premium": "sell", "thesis": "bearish, sell rich vol", "ivNote": iv_note}
        if cheap:
            return {"structure": "put debit spread", "premium": "buy", "thesis": "bearish, buy cheap downside", "ivNote": iv_note}
        return {"structure": "put debit spread", "premium": "buy", "thesis": "bearish directional", "ivNote": iv_note}
    return {"structure": "calendar / wait", "premium": "neutral", "thesis": "timing risk — wait for confirmation", "ivNote": iv_note}


def build_trade_ideas(verdict: TickerVerdict, *, orats_client: Any = None) -> List[Dict[str, Any]]:
    """1-2 trade expressions for one verdict (empty if not actionable)."""
    if not verdict.is_actionable or verdict.direction == "neutral":
        if verdict.label == models.LABEL_DELAYED:
            return [{
                "type": "watch",
                "ticker": verdict.ticker,
                "direction": "neutral",
                "expression": "Watchlist — real thesis, wrong quarter. Set an alert for the "
                              "next capex datapoint / interconnection milestone before sizing.",
            }]
        return []

    ideas: List[Dict[str, Any]] = []
    side = "Long" if verdict.direction == "long" else "Short"
    ideas.append({
        "type": "directional",
        "ticker": verdict.ticker,
        "direction": verdict.direction,
        "expression": f"{side} {verdict.ticker} — {models.LABEL_DISPLAY.get(verdict.label, verdict.label)} "
                      f"(conviction {verdict.conviction:.0f}).",
    })

    opt = _option_structure(verdict.direction, _iv_rank(verdict.ticker, orats_client))
    # Tie the structure to the horizon: pick an expiry that covers the catalyst.
    h = verdict.horizon or {}
    expiry_hint = ""
    if h.get("catalystDate"):
        expiry_hint = f" — expiry just after {h['catalystDate']} (covers the print)"
    elif h.get("band"):
        expiry_hint = f" — size to a {h['band']} horizon"
    ideas.append({
        "type": "options",
        "ticker": verdict.ticker,
        "direction": verdict.direction,
        "structure": opt["structure"],
        "expression": f"{verdict.ticker} {opt['structure']} ({opt['thesis']}; {opt['ivNote']}){expiry_hint}.",
        "ivNote": opt["ivNote"],
    })
    return ideas


def build_baskets(verdicts: List[TickerVerdict]) -> List[Dict[str, Any]]:
    """Category baskets where >=2 names agree on a tradable direction."""
    by_cat_dir: Dict[tuple, List[TickerVerdict]] = {}
    for v in verdicts:
        if not v.is_actionable or v.direction not in ("long", "short"):
            continue
        by_cat_dir.setdefault((v.category, v.direction), []).append(v)

    baskets: List[Dict[str, Any]] = []
    for (cat, direction), members in by_cat_dir.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda v: -v.conviction)
        avg_conv = sum(m.conviction for m in members) / len(members)
        baskets.append({
            "category": cat,
            "categoryName": models.category_name(cat),
            "direction": direction,
            "tickers": [m.ticker for m in members],
            "avgConviction": round(avg_conv, 1),
            "expression": f"{'Long' if direction == 'long' else 'Short'} {models.category_name(cat)} basket: "
                          + ", ".join(m.ticker for m in members)
                          + f" (avg conviction {avg_conv:.0f}).",
        })
    baskets.sort(key=lambda b: -b["avgConviction"])
    return baskets


def attach_trades(verdicts: List[TickerVerdict], *, orats_client: Any = None) -> List[Dict[str, Any]]:
    """Attach per-verdict trade ideas in place and return the basket list."""
    for v in verdicts:
        try:
            v.trade_ideas = build_trade_ideas(v, orats_client=orats_client)
        except Exception as exc:  # pragma: no cover - defensive
            LOG.debug("trade idea build failed for %s: %s", v.ticker, exc)
            v.trade_ideas = []
    return build_baskets(verdicts)
