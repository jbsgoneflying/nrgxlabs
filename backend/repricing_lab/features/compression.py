"""Compression / expansion features (NR7, range contraction, volume dry-up)."""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence


def compute_compression(bars: Sequence[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    if len(bars) < 20:
        return {
            "range_pct": None, "nr7": None, "inside_day": None,
            "bb_width_pctile": None, "rvol20": None, "volume_dryup": None,
        }
    ranges = []
    for b in bars:
        h, l = b.get("high"), b.get("low")
        c = b.get("adjusted_close") if b.get("adjusted_close") is not None else b.get("close")
        if h is None or l is None or not c:
            ranges.append(None)
        else:
            ranges.append((float(h) - float(l)) / float(c))
    last_range = ranges[-1]
    window7 = [r for r in ranges[-7:] if r is not None]
    nr7 = None
    if last_range is not None and len(window7) == 7:
        nr7 = 1.0 if last_range == min(window7) else 0.0
    inside = None
    if len(bars) >= 2 and bars[-1].get("high") is not None and bars[-2].get("high") is not None:
        inside = 1.0 if (
            float(bars[-1]["high"]) <= float(bars[-2]["high"])
            and float(bars[-1]["low"]) >= float(bars[-2]["low"])
        ) else 0.0
    vols = [float(b.get("volume") or 0) for b in bars[-20:]]
    avg_vol = sum(vols) / len(vols) if vols else None
    last_vol = float(bars[-1].get("volume") or 0)
    rvol = (last_vol / avg_vol) if avg_vol else None
    dry = 1.0 if rvol is not None and rvol < 0.7 else (0.0 if rvol is not None else None)
    # Bollinger width percentile (simple): std of closes / mean
    closes = []
    for b in bars[-20:]:
        px = b.get("adjusted_close") if b.get("adjusted_close") is not None else b.get("close")
        if px is not None:
            closes.append(float(px))
    bb_width = None
    if len(closes) >= 20:
        mean = sum(closes) / len(closes)
        var = sum((x - mean) ** 2 for x in closes) / len(closes)
        std = var ** 0.5
        bb_width = (2 * std) / mean if mean else None
    return {
        "range_pct": last_range,
        "nr7": nr7,
        "inside_day": inside,
        "bb_width": bb_width,
        "rvol20": rvol,
        "volume_dryup": dry,
    }
