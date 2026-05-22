"""Tests for backend.engine2b.weekend_stress.

We pin:

- Term-spread classification ladder (NORMAL/MODERATE/ELEVATED/SEVERE).
- 25Δ put skew classification ladder.
- Composite "worst-of" aggregation.
- IV normalization (decimal vs already-percent inputs).
- ``next_friday_after`` correctness across weekdays + Friday edge case.
- Live-chain stub flow: chain pull → ATM IV → skew → composite level.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List

import pytest

from backend.engine2b.weekend_stress import (
    _atm_iv,
    _classify,
    _composite_level,
    _normalize_iv_pct,
    _put_skew_25d_pts,
    _SKEW_THRESHOLDS,
    _TERM_THRESHOLDS,
    compute_weekend_stress,
    next_friday_after,
)


def test_next_friday_after_basic_weekdays():
    # Tuesday 5/26 -> Friday 5/29
    assert next_friday_after(dt.date(2026, 5, 26)) == dt.date(2026, 5, 29)
    # Wednesday 5/27 -> Friday 5/29
    assert next_friday_after(dt.date(2026, 5, 27)) == dt.date(2026, 5, 29)
    # Sunday 5/24 -> Friday 5/29
    assert next_friday_after(dt.date(2026, 5, 24)) == dt.date(2026, 5, 29)


def test_next_friday_after_friday_advances_to_next_friday():
    """Same-day Friday must advance to the FOLLOWING Friday so the
    comparison expiry is always strictly later than the trade's expiry."""
    assert next_friday_after(dt.date(2026, 5, 29)) == dt.date(2026, 6, 5)


def test_normalize_iv_handles_decimal_and_percent_inputs():
    # ORATS decimal: 0.125 -> 12.5%
    assert _normalize_iv_pct(0.125) == 12.5
    # Already percent: 12.5 -> 12.5
    assert _normalize_iv_pct(12.5) == 12.5
    # Edge: 1.49 still treated as decimal (149%)
    assert _normalize_iv_pct(1.49) == 149.0
    # Edge: 1.51 already percent
    assert _normalize_iv_pct(1.51) == 1.51
    # Invalid inputs return None
    assert _normalize_iv_pct(None) is None
    assert _normalize_iv_pct(0) is None
    assert _normalize_iv_pct(-0.1) is None


def test_term_classification_ladder():
    # SEVERE at +3pt or higher
    assert _classify(3.0, _TERM_THRESHOLDS) == "SEVERE"
    assert _classify(5.5, _TERM_THRESHOLDS) == "SEVERE"
    # ELEVATED at +1pt to +3pt
    assert _classify(1.0, _TERM_THRESHOLDS) == "ELEVATED"
    assert _classify(2.5, _TERM_THRESHOLDS) == "ELEVATED"
    # MODERATE at -1pt to +1pt
    assert _classify(0.0, _TERM_THRESHOLDS) == "MODERATE"
    assert _classify(-1.0, _TERM_THRESHOLDS) == "MODERATE"
    # NORMAL below -1pt (longer-dated meaningfully above near)
    assert _classify(-2.0, _TERM_THRESHOLDS) == "NORMAL"
    assert _classify(-5.0, _TERM_THRESHOLDS) == "NORMAL"
    # UNKNOWN for None
    assert _classify(None, _TERM_THRESHOLDS) == "UNKNOWN"


def test_skew_classification_ladder():
    assert _classify(8.0, _SKEW_THRESHOLDS) == "SEVERE"
    assert _classify(12.0, _SKEW_THRESHOLDS) == "SEVERE"
    assert _classify(5.0, _SKEW_THRESHOLDS) == "ELEVATED"
    assert _classify(7.5, _SKEW_THRESHOLDS) == "ELEVATED"
    assert _classify(3.0, _SKEW_THRESHOLDS) == "MODERATE"
    assert _classify(4.0, _SKEW_THRESHOLDS) == "MODERATE"
    assert _classify(2.0, _SKEW_THRESHOLDS) == "NORMAL"
    assert _classify(0.5, _SKEW_THRESHOLDS) == "NORMAL"
    assert _classify(None, _SKEW_THRESHOLDS) == "UNKNOWN"


def test_composite_level_is_worst_of():
    """One alarming signal should be enough to flip the composite."""
    assert _composite_level("NORMAL", "NORMAL") == "NORMAL"
    assert _composite_level("NORMAL", "MODERATE") == "MODERATE"
    assert _composite_level("MODERATE", "NORMAL") == "MODERATE"
    assert _composite_level("ELEVATED", "NORMAL") == "ELEVATED"
    assert _composite_level("NORMAL", "SEVERE") == "SEVERE"
    assert _composite_level("SEVERE", "ELEVATED") == "SEVERE"
    # UNKNOWN is dropped when the other side has a real reading.
    assert _composite_level("UNKNOWN", "ELEVATED") == "ELEVATED"
    assert _composite_level("UNKNOWN", "UNKNOWN") == "UNKNOWN"


def _row(strike: float, *, civ: float, piv: float, pdelta: float, cdelta: float = None) -> Dict[str, Any]:
    return {
        "strike": float(strike),
        "callMidIv": float(civ),
        "putMidIv": float(piv),
        "putDelta": float(pdelta),
        "callDelta": float(cdelta) if cdelta is not None else max(0.0, 1.0 + float(pdelta)),
    }


def test_atm_iv_picks_strike_nearest_spot():
    rows = [
        _row(5500, civ=0.10, piv=0.10, pdelta=-0.45),
        _row(5550, civ=0.11, piv=0.13, pdelta=-0.50),  # spot 5555 -> this wins
        _row(5600, civ=0.09, piv=0.09, pdelta=-0.30),
    ]
    iv = _atm_iv(rows, 5555.0)
    # ORATS decimals: avg(0.11, 0.13) = 0.12 -> 12.0%
    assert iv == 12.0


def test_put_skew_picks_25delta_put():
    rows = [
        _row(5400, civ=0.10, piv=0.18, pdelta=-0.10),
        _row(5500, civ=0.10, piv=0.15, pdelta=-0.25),  # 25-delta -> winner
        _row(5600, civ=0.10, piv=0.10, pdelta=-0.50),  # ATM
    ]
    # ATM IV (anchored at spot 5600) = (0.10 + 0.10)/2 = 10.0%
    skew = _put_skew_25d_pts(rows, atm_iv_pct=10.0)
    # 25Δ put IV = 0.15 -> 15.0%, skew = 15.0 - 10.0 = 5.0
    assert skew == 5.0


class _Resp:
    def __init__(self, rows):
        self.rows = rows


class _FakeClient:
    def __init__(self, chains: Dict[str, List[Dict[str, Any]]]):
        self._chains = chains

    def live_strikes_by_expiry(self, *, ticker: str, expiry: str, fields: str):
        return _Resp(self._chains.get((ticker, expiry), []))

    def live_strikes(self, *, ticker: str, fields: str):
        rows = []
        for (sym, _exp), r in self._chains.items():
            if sym == ticker:
                rows.extend(r)
        return _Resp(rows)


def _chain_for(*, expiry: str, atm_iv: float, put_25d_iv: float, spot: float = 5550.0) -> List[Dict[str, Any]]:
    return [
        # Spot anchor strike (ATM)
        {
            "strike": spot, "expirDate": expiry,
            "spotPrice": spot, "stockPrice": spot,
            "callMidIv": atm_iv, "putMidIv": atm_iv,
            "putDelta": -0.50, "callDelta": 0.50,
        },
        # 25Δ put strike
        {
            "strike": spot - 50.0, "expirDate": expiry,
            "spotPrice": spot, "stockPrice": spot,
            "callMidIv": atm_iv, "putMidIv": put_25d_iv,
            "putDelta": -0.25, "callDelta": 0.75,
        },
    ]


def test_compute_weekend_stress_normal_regime():
    """Term curve normal (longer-dated higher) + benign skew -> NORMAL."""
    chains = {
        ("SPXW", "2026-05-26"): _chain_for(expiry="2026-05-26", atm_iv=0.10, put_25d_iv=0.12),
        ("SPXW", "2026-05-29"): _chain_for(expiry="2026-05-29", atm_iv=0.12, put_25d_iv=0.14),
    }
    client = _FakeClient(chains)
    out = compute_weekend_stress(
        client,
        ticker="SPX",
        today=dt.date(2026, 5, 22),
        near_expiry=dt.date(2026, 5, 26),
    )
    assert out["enabled"] is True
    assert out["nearAtmIvPct"] == 10.0
    assert out["comparisonAtmIvPct"] == 12.0
    # Term spread = 10 - 12 = -2.0pts -> NORMAL
    assert out["termSpreadPts"] == -2.0
    assert out["termLevel"] == "NORMAL"
    # Skew = 12 - 10 = 2pts -> NORMAL
    assert out["put25dSkewPts"] == 2.0
    assert out["skewLevel"] == "NORMAL"
    assert out["level"] == "NORMAL"


def test_compute_weekend_stress_severe_term_inversion():
    """Front-week IV well above next-Friday => SEVERE term, composite SEVERE."""
    chains = {
        ("SPXW", "2026-05-26"): _chain_for(expiry="2026-05-26", atm_iv=0.18, put_25d_iv=0.20),
        ("SPXW", "2026-05-29"): _chain_for(expiry="2026-05-29", atm_iv=0.14, put_25d_iv=0.16),
    }
    client = _FakeClient(chains)
    out = compute_weekend_stress(
        client,
        ticker="SPX",
        today=dt.date(2026, 5, 22),
        near_expiry=dt.date(2026, 5, 26),
    )
    # Term spread = 18 - 14 = +4pts -> SEVERE
    assert out["termSpreadPts"] == 4.0
    assert out["termLevel"] == "SEVERE"
    assert out["level"] == "SEVERE"
    # Recommendation note must call out SKIP for SEVERE.
    rec_notes = [n for n in out["notes"] if "RECOMMENDATION" in n.upper()]
    assert rec_notes and "SKIP" in rec_notes[0].upper()


def test_compute_weekend_stress_severe_skew():
    """Term curve fine but 25Δ put skew is hedge-panic => composite SEVERE."""
    chains = {
        ("SPXW", "2026-05-26"): _chain_for(expiry="2026-05-26", atm_iv=0.10, put_25d_iv=0.20),
        ("SPXW", "2026-05-29"): _chain_for(expiry="2026-05-29", atm_iv=0.12, put_25d_iv=0.14),
    }
    client = _FakeClient(chains)
    out = compute_weekend_stress(
        client,
        ticker="SPX",
        today=dt.date(2026, 5, 22),
        near_expiry=dt.date(2026, 5, 26),
    )
    # Skew = 20 - 10 = +10pts -> SEVERE
    assert out["put25dSkewPts"] == 10.0
    assert out["skewLevel"] == "SEVERE"
    # Term is fine (-2pts).
    assert out["termLevel"] == "NORMAL"
    # Worst-of -> SEVERE
    assert out["level"] == "SEVERE"


def test_compute_weekend_stress_disabled_when_chain_missing():
    client = _FakeClient({})
    out = compute_weekend_stress(
        client,
        ticker="SPX",
        today=dt.date(2026, 5, 22),
        near_expiry=dt.date(2026, 5, 26),
    )
    assert out["enabled"] is False
    assert out["level"] == "UNKNOWN"
    assert any("unavailable" in n.lower() for n in out["warnings"])
