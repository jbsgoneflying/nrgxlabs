"""Post-event acceptance features (gap size, gap retention, VWAP relation)."""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence


def compute_acceptance(
    bars: Sequence[Dict[str, Any]],
    *,
    event_session: Optional[str] = None,
) -> Dict[str, Optional[float]]:
    """Gap and acceptance metrics around an event session.

    If ``event_session`` is provided, find that bar; else use last two bars.
    """
    if len(bars) < 2:
        return {"gap_pct": None, "gap_retention": None, "close_vs_event_vwap": None}
    idx = len(bars) - 1
    if event_session:
        for i, b in enumerate(bars):
            if str(b.get("session_date") or b.get("date") or "")[:10] == event_session[:10]:
                idx = i
                break
    if idx < 1:
        return {"gap_pct": None, "gap_retention": None, "close_vs_event_vwap": None}
    prev = bars[idx - 1]
    cur = bars[idx]
    prev_c = prev.get("adjusted_close") if prev.get("adjusted_close") is not None else prev.get("close")
    cur_o = cur.get("open")
    cur_c = cur.get("adjusted_close") if cur.get("adjusted_close") is not None else cur.get("close")
    if not prev_c or not cur_o:
        return {"gap_pct": None, "gap_retention": None, "close_vs_event_vwap": None}
    gap = (float(cur_o) / float(prev_c)) - 1.0
    retention = None
    if cur_c is not None and gap != 0:
        # Fraction of gap retained into close
        move = (float(cur_c) / float(prev_c)) - 1.0
        retention = move / gap if abs(gap) > 1e-12 else None
    # Event VWAP proxy: (H+L+C)/3
    h, l = cur.get("high"), cur.get("low")
    vwap = None
    if h is not None and l is not None and cur_c is not None:
        vwap = (float(h) + float(l) + float(cur_c)) / 3.0
    close_vs = ((float(cur_c) / vwap) - 1.0) if vwap and cur_c else None
    return {
        "gap_pct": gap,
        "gap_retention": retention,
        "close_vs_event_vwap": close_vs,
    }
