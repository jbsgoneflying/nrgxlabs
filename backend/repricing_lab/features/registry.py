"""Feature registry — lists Phase-1 feature names and roles."""
from __future__ import annotations

from typing import Dict

# role: filter | ranker | diagnostic | risk_veto
FEATURE_ROLES: Dict[str, str] = {
    "ret_1m": "ranker",
    "ret_3m": "ranker",
    "ret_6m": "ranker",
    "ret_12m": "ranker",
    "dist_sma20": "diagnostic",
    "dist_sma50": "filter",
    "dist_sma200": "filter",
    "dist_52w_high": "ranker",
    "dist_52w_low": "diagnostic",
    "atr14": "risk_veto",
    "atr_pct": "risk_veto",
    "last_price": "filter",
    "rs_vs_spy": "ranker",
    "ret_bench": "diagnostic",
    "range_pct": "diagnostic",
    "nr7": "filter",
    "inside_day": "diagnostic",
    "bb_width": "filter",
    "rvol20": "filter",
    "volume_dryup": "filter",
    "gap_pct": "filter",
    "gap_retention": "ranker",
    "close_vs_event_vwap": "ranker",
    "eps_surprise": "filter",
    "estimate_is_pit": "diagnostic",
    "earnings_timing_bmo": "diagnostic",
    "adv20_usd": "risk_veto",
    "dollar_volume_last": "diagnostic",
    "gap_stress_q90": "risk_veto",
    "stop_distance_pct": "risk_veto",
}
