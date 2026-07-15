"""Risk / liquidity features."""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence


def compute_risk_liquidity(bars: Sequence[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    if len(bars) < 5:
        return {"adv20_usd": None, "dollar_volume_last": None}
    dollars = []
    for b in bars[-20:]:
        px = b.get("adjusted_close") if b.get("adjusted_close") is not None else b.get("close")
        vol = b.get("volume") or 0
        if px is not None:
            dollars.append(float(px) * float(vol))
    adv = sum(dollars) / len(dollars) if dollars else None
    last_px = bars[-1].get("adjusted_close")
    if last_px is None:
        last_px = bars[-1].get("close")
    last_dv = (float(last_px) * float(bars[-1].get("volume") or 0)) if last_px else None
    return {"adv20_usd": adv, "dollar_volume_last": last_dv}
