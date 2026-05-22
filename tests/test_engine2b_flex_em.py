"""Tests for backend.engine2b.flex_em.

Confirms the EM picker accepts an arbitrary expiry (e.g. Tue 2026-05-26
after Memorial Day) without coercing back to a Friday.
"""
from __future__ import annotations

import datetime as dt
from typing import List

import pytest

from backend.engine2b.flex_em import compute_expected_move_flex


class _Resp:
    def __init__(self, rows: List[dict]):
        self.rows = rows
        self.raw = rows


class _LiveClient:
    """Minimal stub of the live ORATS surface flex_em.py reaches for."""

    def __init__(self, *, expiry: str, spot: float, by_strike: dict):
        self._expiry = expiry
        self._spot = float(spot)
        self._rows = []
        for k, (cb, ca, pb, pa) in by_strike.items():
            self._rows.append({
                "ticker": "SPXW",
                "tradeDate": "2026-05-22",
                "expirDate": expiry,
                "strike": float(k),
                "spotPrice": self._spot,
                "stockPrice": self._spot,
                "callBidPrice": float(cb),
                "callAskPrice": float(ca),
                "putBidPrice": float(pb),
                "putAskPrice": float(pa),
                "callOpenInterest": 1000,
                "putOpenInterest": 1000,
            })

    def live_expirations(self, *, ticker: str):
        return _Resp([{"expirDate": self._expiry}])

    def live_strikes_by_expiry(self, *, ticker: str, expiry: str, fields: str):
        rows = self._rows if str(expiry)[:10] == self._expiry else []
        return _Resp(rows)

    def live_strikes(self, *, ticker: str, fields: str):
        return _Resp(self._rows)


def _toy_atm_chain(spot: float, em_pct: float):
    """Build a strike grid where the ATM straddle prices the requested EM%.

    Set ATM straddle = spot * em_pct / 100 (1-sigma ≈ straddle PV for short DTE).
    OTM legs are zero-bid placeholders to keep the forward calc trivial.
    """
    straddle_mid = spot * em_pct / 100.0
    half_call = straddle_mid / 2.0
    half_put = straddle_mid / 2.0
    grid = {}
    for k_off in (-50.0, -25.0, 0.0, 25.0, 50.0):
        k = spot + k_off
        if k_off == 0.0:
            grid[k] = (half_call - 0.5, half_call + 0.5, half_put - 0.5, half_put + 0.5)
        elif k_off > 0:
            # OTM call has some value, OTM put almost zero
            grid[k] = (max(0.0, half_call - k_off - 1.0), max(0.5, half_call - k_off + 1.0), 0.05, 0.10)
        else:
            grid[k] = (0.05, 0.10, max(0.0, half_put + k_off - 1.0), max(0.5, half_put + k_off + 1.0))
    return grid


def test_flex_em_uses_requested_expiry_not_friday():
    """Tue 2026-05-26 after Memorial Day must come back verbatim — no Friday coercion."""
    spot = 5000.0
    target_expiry = "2026-05-26"  # Tuesday
    chain = _toy_atm_chain(spot, em_pct=1.0)
    client = _LiveClient(expiry=target_expiry, spot=spot, by_strike=chain)

    result = compute_expected_move_flex(
        client,
        ticker="SPX",
        today=dt.date(2026, 5, 22),
        expiry=dt.date(2026, 5, 26),
    )

    assert result["expiry"] == target_expiry
    assert dt.date.fromisoformat(result["expiry"]).weekday() == 1  # Tuesday, not Friday
    assert result["dte"] == 4
    em_pct = result.get("expectedMovePct")
    assert em_pct is not None
    assert em_pct > 0, f"Expected positive EM, got {em_pct}"


def test_flex_em_rejects_non_positive_dte():
    """Same-day or past expiry must return an empty result, not blow up."""
    spot = 5000.0
    chain = _toy_atm_chain(spot, em_pct=1.0)
    client = _LiveClient(expiry="2026-05-22", spot=spot, by_strike=chain)
    result = compute_expected_move_flex(
        client,
        ticker="SPX",
        today=dt.date(2026, 5, 22),
        expiry=dt.date(2026, 5, 22),
    )
    assert result["expectedMovePct"] in (None, 0.0)
    assert any("on or before" in w for w in result.get("warnings", []))


def test_flex_em_handles_client_without_live_methods():
    """If the client lacks live_* methods (e.g. mock client), we must not crash."""

    class _BareClient:
        pass

    result = compute_expected_move_flex(
        _BareClient(),
        ticker="SPX",
        today=dt.date(2026, 5, 22),
        expiry=dt.date(2026, 5, 26),
    )
    assert result["expectedMovePct"] is None
    assert result["expiry"] == "2026-05-26"
    assert any("No usable chain found" in w for w in result.get("warnings", []))
