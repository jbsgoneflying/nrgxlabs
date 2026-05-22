"""Tests for backend.engine2b.flex_live_chain.

We stub the ORATS client with a deterministic chain so we can pin:

- Strike snap-to-nearest math for both SPX ($5 grid) and SPY ($1 grid).
- Mid credit / max loss / ROC / POP plumbing.
- Graceful degrade when the chain is empty / mid prices unavailable.
- Symbol fallback (SPXW -> SPX -> SPY) when the first option has no rows.
"""
from __future__ import annotations

import datetime as dt
from typing import List

from backend.engine2b.flex_live_chain import compute_flex_live_chain_targets


class _Resp:
    def __init__(self, rows: List[dict]):
        self.rows = rows


class FakeChainClient:
    """Stub that returns canned strike rows for a single (symbol, expiry)."""

    def __init__(self):
        self._chains: dict = {}

    def add_chain(self, symbol: str, expiry: str, *, spot: float, strikes: List[dict]):
        self._chains[(symbol, expiry)] = (spot, list(strikes))

    def live_strikes_by_expiry(self, *, ticker: str, expiry: str, fields: str):
        spot, rows = self._chains.get((ticker, expiry), (None, []))
        if not rows:
            return _Resp([])
        out = []
        for r in rows:
            r2 = dict(r)
            r2["spotPrice"] = spot
            r2["stockPrice"] = spot
            r2["expirDate"] = expiry
            out.append(r2)
        return _Resp(out)

    def live_strikes(self, *, ticker: str, fields: str):
        # Concatenate all expiries we have for this ticker (used by the
        # fallback path inside compute_flex_live_chain_targets).
        out = []
        for (sym, exp), (spot, rows) in self._chains.items():
            if sym != ticker:
                continue
            for r in rows:
                r2 = dict(r)
                r2["spotPrice"] = spot
                r2["stockPrice"] = spot
                r2["expirDate"] = exp
                out.append(r2)
        return _Resp(out)


def _spx_strike(strike: float, *, put_bid: float, put_ask: float, call_bid: float, call_ask: float,
                put_delta: float = -0.05, call_delta: float = 0.05) -> dict:
    return {
        "strike": float(strike),
        "putBidPrice": float(put_bid),
        "putAskPrice": float(put_ask),
        "callBidPrice": float(call_bid),
        "callAskPrice": float(call_ask),
        "putDelta": float(put_delta),
        "callDelta": float(call_delta),
    }


def test_live_chain_snaps_to_nearest_strike_and_computes_credit():
    """Spot 5550, EM 1.0%, 2.5x EM short distance ~ 5550 * 0.025 = 138.75 pts.
    Targets: short put ~ 5411, short call ~ 5689. SPX $5 grid snaps to
    5410 (put) and 5690 (call). Wing $5 => long put 5405, long call 5695.

    Set bid/ask midpoints so the math is easy:
      short put mid = 0.50, short call mid = 0.40
      long put mid  = 0.20, long call mid = 0.18
      Net credit = 0.50 + 0.40 - 0.20 - 0.18 = 0.52
      Max loss = 5 - 0.52 = 4.48
      ROC = 0.52 / 4.48 = 11.61%
    """
    client = FakeChainClient()
    expiry = "2026-05-26"
    strikes = [
        _spx_strike(5405, put_bid=0.10, put_ask=0.30, call_bid=144.0, call_ask=145.0),
        _spx_strike(5410, put_bid=0.40, put_ask=0.60, call_bid=139.0, call_ask=140.0),
        _spx_strike(5550, put_bid=4.00, put_ask=4.20, call_bid=4.00, call_ask=4.20),
        _spx_strike(5690, put_bid=139.0, put_ask=140.0, call_bid=0.30, call_ask=0.50),
        _spx_strike(5695, put_bid=144.0, put_ask=145.0, call_bid=0.08, call_ask=0.28),
    ]
    client.add_chain("SPXW", expiry, spot=5550.0, strikes=strikes)

    out = compute_flex_live_chain_targets(
        client,
        ticker="SPX",
        today=dt.date(2026, 5, 22),
        expiry=dt.date(2026, 5, 26),
        em_pct=1.0,
        em_mults=[2.5],
        wing_pts=[5],
    )
    assert out["enabled"] is True
    assert out["symbolUsed"] == "SPXW"
    assert out["spotPrice"] == 5550.0
    assert len(out["targets"]) == 1
    t = out["targets"][0]
    assert t["shortPut"] == 5410
    assert t["longPut"] == 5405
    assert t["shortCall"] == 5690
    assert t["longCall"] == 5695
    # Mid prices snap to the bid/ask midpoint.
    assert t["shortPutMid"] == 0.5
    assert t["shortCallMid"] == 0.4
    assert t["longPutMid"] == 0.2
    assert t["longCallMid"] == 0.18
    # 0.50 + 0.40 - 0.20 - 0.18 = 0.52
    assert abs(t["netMidCredit"] - 0.52) < 1e-6
    assert abs(t["maxLossPerContract"] - 4.48) < 1e-6
    assert t["rocPct"] is not None and t["rocPct"] > 10.0
    # POP from short deltas = 1 - (0.05 + 0.05) = 0.90
    assert t["popFromMid"] is not None and abs(t["popFromMid"] - 0.90) < 1e-6


def test_live_chain_disabled_when_no_chain_available():
    client = FakeChainClient()
    out = compute_flex_live_chain_targets(
        client,
        ticker="SPX",
        today=dt.date(2026, 5, 22),
        expiry=dt.date(2026, 5, 26),
        em_pct=1.0,
        em_mults=[2.5],
        wing_pts=[5],
    )
    assert out["enabled"] is False
    assert out["targets"] == []
    assert any("no usable live chain" in n.lower() or "no live targets" in n.lower() for n in out["warnings"])


def test_live_chain_disabled_when_em_pct_invalid():
    client = FakeChainClient()
    out = compute_flex_live_chain_targets(
        client,
        ticker="SPX",
        today=dt.date(2026, 5, 22),
        expiry=dt.date(2026, 5, 26),
        em_pct=0.0,
        em_mults=[1.0, 2.5],
        wing_pts=[5],
    )
    assert out["enabled"] is False
    assert any("em unavailable" in (w or "").lower() for w in out["warnings"])


def test_live_chain_falls_back_to_spy_when_spxw_empty():
    """If SPXW returns nothing, the helper should walk SPX then SPY.
    SPY $1 grid => spot 555, 2.5x EM at 1.0% => 555 * 0.025 = 13.875,
    snaps to short put 541 and short call 569. Wing 1 => long put 540,
    long call 570.
    """
    client = FakeChainClient()
    expiry = "2026-05-26"
    spy_strikes = []
    for k in (540, 541, 555, 569, 570):
        spy_strikes.append({
            "strike": float(k),
            "putBidPrice": 0.10, "putAskPrice": 0.20,
            "callBidPrice": 0.10, "callAskPrice": 0.20,
            "putDelta": -0.03, "callDelta": 0.03,
        })
    client.add_chain("SPY", expiry, spot=555.0, strikes=spy_strikes)

    out = compute_flex_live_chain_targets(
        client,
        ticker="SPX",
        today=dt.date(2026, 5, 22),
        expiry=dt.date(2026, 5, 26),
        em_pct=1.0,
        em_mults=[2.5],
        wing_pts=[1],
    )
    assert out["enabled"] is True
    assert out["symbolUsed"] == "SPY"
    assert out["spotPrice"] == 555.0
    assert out["targets"], "expected at least one resolved target on SPY proxy"
