"""Point-in-time universe tier builder from daily bars (+ reason codes)."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.config import get_flags
from backend.repricing_lab import store

LOG = logging.getLogger("repricing_lab.universe_pit")

BUILDER_VERSION = "universe_pit-v1"


def _adv_usd(bars: Sequence[Dict[str, Any]], window: int) -> Optional[float]:
    if len(bars) < window:
        return None
    slice_ = bars[-window:]
    dollars = []
    for b in slice_:
        px = b.get("adjusted_close") if b.get("adjusted_close") is not None else b.get("close")
        vol = b.get("volume") or 0.0
        if px is None:
            continue
        dollars.append(float(px) * float(vol))
    if len(dollars) < max(1, window // 2):
        return None
    return sum(dollars) / len(dollars)


def classify_tier(
    *,
    price: Optional[float],
    adv20: Optional[float],
    adv60: Optional[float],
    min_price: float,
    t1_min_adv: float,
    t2_min_adv: float,
    etf: bool = False,
    active: bool = True,
) -> Tuple[Optional[str], List[str], bool, bool]:
    """Return (tier|None, exclusion_reasons, eligible_long, eligible_short).

    Short eligibility is always False in Phase 1 (no borrow data).
    """
    reasons: List[str] = []
    if not active:
        reasons.append("delisted")
    if etf:
        reasons.append("etf_excluded")
    if price is None:
        reasons.append("missing_price")
    elif price < min_price:
        reasons.append("price_below_floor")
    if adv20 is None and adv60 is None:
        reasons.append("insufficient_adv_history")
    adv = adv20 if adv20 is not None else adv60
    if adv is not None and adv < t2_min_adv:
        reasons.append("adv_below_t2")

    if reasons:
        return None, reasons, False, False

    assert price is not None and adv is not None
    if adv >= t1_min_adv:
        return "tier1_liquid_core", [], True, False
    if adv >= t2_min_adv:
        return "tier2_satellite", [], True, False
    return None, ["adv_below_t2"], False, False


def build_universe_snapshot(
    conn,
    *,
    snapshot_date: str,
    as_of: Optional[str] = None,
    instrument_ids: Optional[Sequence[str]] = None,
) -> int:
    """Build PIT universe rows for ``snapshot_date`` from bars available as-of.

    Returns number of eligible (long) rows written across tiers.
    """
    flags = get_flags()
    min_price = float(flags.REPRICING_LAB_UNIVERSE_MIN_PRICE)
    t1 = float(flags.REPRICING_LAB_T1_MIN_ADV_USD)
    t2 = float(flags.REPRICING_LAB_T2_MIN_ADV_USD)
    as_of = as_of or f"{snapshot_date}T23:59:59Z"

    if instrument_ids is None:
        instruments = store.read_rows(conn, "instrument_master")
    else:
        instruments = []
        for iid in instrument_ids:
            instruments.extend(
                store.read_rows(conn, "instrument_master", where="instrument_id = ?", params=(iid,))
            )

    rows_out: List[Dict[str, Any]] = []
    for inst in instruments:
        iid = inst["instrument_id"]
        bars = store.read_available(
            conn,
            "daily_bar",
            as_of=as_of,
            where="instrument_id = ? AND session_date <= ?",
            params=(iid, snapshot_date),
            order_by="session_date ASC",
        )
        price = None
        if bars:
            last = bars[-1]
            price = last.get("adjusted_close") if last.get("adjusted_close") is not None else last.get("close")
        adv20 = _adv_usd(bars, 20)
        adv60 = _adv_usd(bars, 60)
        tier, reasons, elig_long, elig_short = classify_tier(
            price=price,
            adv20=adv20,
            adv60=adv60,
            min_price=min_price,
            t1_min_adv=t1,
            t2_min_adv=t2,
            etf=bool(inst.get("etf_flag")),
            active=bool(inst.get("active_flag", 1)),
        )
        # Always record a row for audit when we have a tier OR exclusions.
        if tier is None:
            # Park exclusions under a synthetic non-tier bucket is avoided —
            # only write eligible tiers; exclusions live in QA diagnostics.
            continue
        rows_out.append({
            "snapshot_date": snapshot_date,
            "instrument_id": iid,
            "universe_tier": tier,
            "price": price,
            "adv20_usd": adv20,
            "adv60_usd": adv60,
            "market_cap": None,
            "eligible_long": 1 if elig_long else 0,
            "eligible_short": 1 if elig_short else 0,
            "exclusion_reasons": json.dumps(reasons),
            "builder_version": BUILDER_VERSION,
            "as_of": as_of,
        })

    if not rows_out:
        return 0
    store.upsert(conn, "universe_snapshot", rows_out)
    return len(rows_out)


def is_eligible_long(conn, instrument_id: str, *, snapshot_date: str, tier: Optional[str] = None) -> bool:
    where = "instrument_id = ? AND snapshot_date = ? AND eligible_long = 1"
    params: List[Any] = [instrument_id, snapshot_date]
    if tier:
        where += " AND universe_tier = ?"
        params.append(tier)
    rows = store.read_rows(conn, "universe_snapshot", where=where, params=params, limit=1)
    return bool(rows)
