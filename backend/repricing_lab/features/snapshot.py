"""Build and persist feature snapshots (leakage-safe via store.read_available)."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from backend.repricing_lab import store
from backend.repricing_lab.features import FEATURE_VERSION
from backend.repricing_lab.features.acceptance import compute_acceptance
from backend.repricing_lab.features.compression import compute_compression
from backend.repricing_lab.features.event_fundamental import compute_event_fundamental
from backend.repricing_lab.features.price_trend import compute_price_trend
from backend.repricing_lab.features.relative_strength import compute_relative_strength
from backend.repricing_lab.features.risk_liquidity import compute_risk_liquidity
from backend.repricing_lab.gap_stress import gap_stress_quantile

LOG = logging.getLogger("repricing_lab.features.snapshot")


def _snapshot_id(instrument_id: str, as_of: str, feature_version: str) -> str:
    raw = f"{instrument_id}|{as_of}|{feature_version}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def build_features_for_instrument(
    conn,
    instrument_id: str,
    *,
    as_of: str,
    event_session: Optional[str] = None,
    earnings: Optional[Dict[str, Any]] = None,
    spy_instrument_id: str = "eodhd:SPY.US",
) -> Dict[str, Any]:
    bars = store.read_available(
        conn, "daily_bar", as_of=as_of,
        where="instrument_id = ?", params=(instrument_id,),
        order_by="session_date ASC",
    )
    # Assert leakage invariant for tests / callers
    for b in bars:
        if str(b["available_at"]) > str(as_of):
            raise RuntimeError(f"lookahead bar {b['session_date']} available_at={b['available_at']} > as_of={as_of}")

    spy_bars = store.read_available(
        conn, "daily_bar", as_of=as_of,
        where="instrument_id = ?", params=(spy_instrument_id,),
        order_by="session_date ASC",
    )
    feats: Dict[str, Any] = {}
    feats.update(compute_price_trend(bars))
    feats.update(compute_relative_strength(bars, spy_bars))
    feats.update(compute_compression(bars))
    feats.update(compute_acceptance(bars, event_session=event_session))
    feats.update(compute_event_fundamental(earnings))
    feats.update(compute_risk_liquidity(bars))
    feats["gap_stress_q90"] = gap_stress_quantile(bars, q=0.90)
    return feats


def persist_feature_snapshot(
    conn,
    instrument_id: str,
    *,
    as_of: str,
    features: Dict[str, Any],
    quality_flags: Optional[List[str]] = None,
) -> str:
    sid = _snapshot_id(instrument_id, as_of, FEATURE_VERSION)
    store.upsert(conn, "feature_snapshot", [{
        "snapshot_id": sid,
        "instrument_id": instrument_id,
        "as_of": as_of,
        "feature_version": FEATURE_VERSION,
        "features_json": json.dumps(features, sort_keys=True, default=str),
        "quality_flags": json.dumps(quality_flags or []),
        "source_versions": json.dumps({"bars": "daily_bar", "features": FEATURE_VERSION}),
        "created_at": store.utcnow_iso(),
    }])
    return sid


def build_feature_snapshots_for_date(conn, *, as_of_date: str) -> int:
    as_of = f"{as_of_date}T23:59:59Z" if "T" not in as_of_date else as_of_date
    instruments = store.read_rows(conn, "instrument_master", where="active_flag = 1")
    n = 0
    for inst in instruments:
        iid = inst["instrument_id"]
        earn = store.read_available(
            conn, "earnings_event", as_of=as_of,
            where="instrument_id = ?", params=(iid,),
            order_by="report_date DESC", limit=1,
        )
        try:
            feats = build_features_for_instrument(
                conn, iid, as_of=as_of,
                earnings=earn[0] if earn else None,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("features failed for %s: %s", iid, exc)
            continue
        persist_feature_snapshot(conn, iid, as_of=as_of, features=feats)
        n += 1
    return n
