"""Engine 15 — build the earnings analogue universe.

Converts Engine 1's ``events[]`` list (the ticker's last ~20 earnings
events, complete with ``earnDate`` / ``anncTod`` / ``impliedMovePct`` /
``realizedMovePct``) into a list of :class:`EarningsWindow` records
shaped for path-dependent replay.

Each window carries:

* ``earn_date_hist`` — the historical earnings announcement date.
* ``entry_date_hist`` — when the hypothetical historical IC would have
  been entered. For AMC events that's ``earn_date`` itself (user enters
  on the day before the AM-pop risk). For BMO events that's
  ``earn_date - 1`` biz day (user enters on the Monday before Tuesday's
  open print).
* ``planned_exit_date_hist`` — where the replay loop stops. Shifted
  forward from ``entry_date_hist`` by the same number of business days
  as ``plannedExitDate - entryDate`` in the user's forward request. For
  the canonical BMO case (user enters the day before, exits the next
  morning) this shifts to ``earn_date_hist`` — the close after the
  announcement.
* ``expiry_date_hist`` — populated later by the chain-replay adapter
  after it probes the cached chain for the closest-DTE expiry to the
  user's requested ``(expiry - entry)`` calendar gap.

The universe is PURE — it does NOT issue any network calls and does NOT
touch the option chain cache. All ORATS work happens in
:mod:`backend.engine15.chain_backfill` and
:mod:`backend.engine15.chain_replay_adapter`.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

LOG = logging.getLogger("engine15.event_universe")


__all__ = [
    "EarningsWindow",
    "build_event_universe",
    "biz_shift",
    "biz_diff",
    "SEASON_BUCKETS",
]


SEASON_BUCKETS = ("Q1", "Q2", "Q3", "Q4")


@dataclass
class EarningsWindow:
    """One historical earnings-analogue for replay."""

    earn_date_hist: str                # ISO YYYY-MM-DD
    annc_tod: str                       # "BMO" | "AMC" | "UNK"
    entry_date_hist: str                # ISO
    planned_exit_date_hist: str         # ISO
    # ``expiry_date_hist`` is optional at universe-build time; the
    # chain-replay adapter resolves it against the cached chain.
    expiry_date_hist: Optional[str] = None
    # Empirical metadata carried straight from E1 for downstream display.
    implied_move_pct: Optional[float] = None
    realized_move_pct: Optional[float] = None
    signed_move_pct: Optional[float] = None
    breach: Optional[bool] = None
    # Derived convenience fields.
    quarter: Optional[str] = None       # "Q1".."Q4" of earn_date_hist
    month: Optional[int] = None
    # Historical spot at entry, populated by the replay adapter after
    # it pulls the entry-day chain (so we can preserve EM-distance when
    # mapping user strikes into the analogue's strike space).
    entry_close: Optional[float] = None
    notes: List[str] = field(default_factory=list)

    def to_meta(self) -> Dict[str, Any]:
        return {
            "earnDate": self.earn_date_hist,
            "anncTod": self.annc_tod,
            "entryDateHist": self.entry_date_hist,
            "plannedExitDateHist": self.planned_exit_date_hist,
            "expiryDateHist": self.expiry_date_hist,
            "impliedMovePct": self.implied_move_pct,
            "realizedMovePct": self.realized_move_pct,
            "signedMovePct": self.signed_move_pct,
            "breach": self.breach,
            "quarter": self.quarter,
            "month": self.month,
            "entryClose": self.entry_close,
            "notes": list(self.notes),
        }


def _parse_iso(s: Any) -> Optional[dt.date]:
    if s is None:
        return None
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def biz_shift(d: dt.date, days: int) -> dt.date:
    """Shift by ``days`` business days.

    v2: when ``ENGINE15_HOLIDAY_CALENDAR`` is on, uses the NYSE trading
    calendar (holidays + half-day handling) from
    :mod:`backend.engine15.trading_calendar`. Falls back to the legacy
    Mon-Fri heuristic when disabled so the desk can A/B the two.
    """
    try:
        from backend.config import get_flags
        if bool(getattr(get_flags(), "ENGINE15_HOLIDAY_CALENDAR", True)):
            from backend.engine15.trading_calendar import add_business_days
            return add_business_days(d, int(days))
    except Exception:
        pass
    # Legacy Mon-Fri heuristic (no holidays).
    step = 1 if days >= 0 else -1
    remaining = abs(int(days))
    cur = d
    while cur.weekday() > 4:
        cur += dt.timedelta(days=step)
    while remaining > 0:
        cur += dt.timedelta(days=step)
        if cur.weekday() <= 4:
            remaining -= 1
    return cur


def biz_diff(a: dt.date, b: dt.date) -> int:
    """Count business days from ``a`` to ``b`` (inclusive endpoints), or
    negative if ``b < a``.

    v2: NYSE holiday-aware when flag is on; Mon-Fri heuristic otherwise.
    """
    if b == a:
        return 0
    sign = 1 if b > a else -1
    x, y = (a, b) if sign > 0 else (b, a)
    try:
        from backend.config import get_flags
        if bool(getattr(get_flags(), "ENGINE15_HOLIDAY_CALENDAR", True)):
            from backend.engine15.trading_calendar import business_days_between
            return sign * business_days_between(x, y)
    except Exception:
        pass
    n = 0
    cur = x + dt.timedelta(days=1)
    while cur <= y:
        if cur.weekday() <= 4:
            n += 1
        cur += dt.timedelta(days=1)
    return sign * n


def _quarter_of(d: dt.date) -> str:
    return f"Q{(d.month - 1) // 3 + 1}"


def _entry_offset_for_timing(annc_tod: str) -> int:
    """Business-day offset from ``earnDate`` to the canonical entry.

    BMO:  user enters the trading day BEFORE the announcement (earnDate-1)
    AMC:  user enters on earnDate itself (announcement after the close)
    UNK:  same as AMC — safest assumption for unknown timing.
    """
    tod = (annc_tod or "").upper()
    if tod == "BMO":
        return -1
    return 0


def _planned_exit_offset_for_timing(annc_tod: str) -> int:
    """Business-day offset from ``entryDate`` to the canonical planned exit.

    BMO:  T+1 biz day after entry (earnDate close) — desk exits in the
          AM session after the overnight print.
    AMC:  T+1 biz day after entry (earnDate+1 close) — same logic.
    UNK:  T+1, conservative.
    """
    return 1


def build_event_universe(
    events: Iterable[Dict[str, Any]],
    *,
    ticker: str,
    user_entry_date: Optional[str] = None,
    user_planned_exit_date: Optional[str] = None,
    user_earnings_date: Optional[str] = None,
) -> List[EarningsWindow]:
    """Build :class:`EarningsWindow` records from E1 ``events[]``.

    The historical planned-exit date is derived by shifting
    ``entry_date_hist`` forward by the same business-day gap as
    ``user_planned_exit_date - user_entry_date`` (so a "enter Mon, exit
    Tue morning" user request maps to each analogue's Mon-to-Tue BMO
    window). If the user gap cannot be inferred, we default to the
    canonical per-timing offset (see :func:`_planned_exit_offset_for_timing`).

    Parameters
    ----------
    events:
        Earnings events from ``backend.earnings_logic.compute_breach_stats``.
    ticker:
        Used only for logging context.
    user_entry_date, user_planned_exit_date:
        Optional. When both are present we honor the user's own biz-day
        gap; otherwise we fall back to the canonical offset.
    user_earnings_date:
        Optional. If the user's ``earnings_date`` differs from their
        ``entry_date`` by some biz-day gap, we also mirror that into
        the historical ``entry_date_hist`` calculation.
    """
    ud_entry = _parse_iso(user_entry_date)
    ud_earn = _parse_iso(user_earnings_date)
    ud_exit = _parse_iso(user_planned_exit_date)

    # How many biz days before earnings does the USER enter?
    user_entry_before_earn: Optional[int] = None
    if ud_entry is not None and ud_earn is not None:
        user_entry_before_earn = biz_diff(ud_entry, ud_earn)

    # How many biz days after entry is the planned exit?
    user_exit_after_entry: Optional[int] = None
    if ud_entry is not None and ud_exit is not None:
        user_exit_after_entry = max(0, biz_diff(ud_entry, ud_exit))

    out: List[EarningsWindow] = []
    for ev in events or []:
        ed = _parse_iso(ev.get("earnDate") or ev.get("earn_date"))
        if ed is None:
            continue
        # ``timing`` holds the classified BMO/AMC/UNK string from Engine 1's
        # ``classify_timing``. ``anncTod`` is the raw ORATS field (e.g.
        # ``"0900"``, ``"06:30:00"``, ``"before market"``) and must only be
        # consulted as a fallback — preferring it over the classified value
        # (as an earlier revision did) forced every single event to the
        # catch-all ``UNK`` bucket and silently broke anncTod parity filtering.
        timing_raw = str(ev.get("timing") or "").strip().upper()
        if timing_raw in ("BMO", "AMC", "UNK") and timing_raw != "":
            timing = timing_raw
        else:
            # Fallback: try the raw anncTod via Engine 1's classifier so we
            # don't lose the signal when a caller passes an unclassified row.
            try:
                from backend.earnings_logic import classify_timing as _classify
                timing = _classify(ev.get("anncTod")) or "UNK"
            except Exception:
                timing = "UNK"
        if timing not in ("BMO", "AMC", "UNK"):
            timing = "UNK"

        # Historical entry date: prefer the user's own entry-vs-earnings
        # gap so a "user enters Monday; earnings Tuesday BMO" request is
        # mirrored as "hist entry = earn_date_hist - 1 biz day".
        if user_entry_before_earn is not None:
            entry_hist = biz_shift(ed, -int(user_entry_before_earn))
        else:
            entry_hist = biz_shift(ed, _entry_offset_for_timing(timing))

        # Historical planned exit: honor the user's entry→exit gap.
        if user_exit_after_entry is not None:
            exit_hist = biz_shift(entry_hist, int(user_exit_after_entry))
        else:
            exit_hist = biz_shift(entry_hist, _planned_exit_offset_for_timing(timing))

        if exit_hist < entry_hist:
            LOG.warning(
                "engine15 universe (%s): planned_exit_date_hist %s before entry %s — skipping.",
                ticker, exit_hist.isoformat(), entry_hist.isoformat(),
            )
            continue

        w = EarningsWindow(
            earn_date_hist=ed.isoformat(),
            annc_tod=timing,
            entry_date_hist=entry_hist.isoformat(),
            planned_exit_date_hist=exit_hist.isoformat(),
            expiry_date_hist=None,
            implied_move_pct=(None if ev.get("impliedMovePct") is None else float(ev.get("impliedMovePct"))),
            realized_move_pct=(None if ev.get("realizedMovePct") is None else float(ev.get("realizedMovePct"))),
            signed_move_pct=(None if ev.get("signedMovePct") is None else float(ev.get("signedMovePct"))),
            breach=(None if ev.get("breach") is None else bool(ev.get("breach"))),
            quarter=_quarter_of(ed),
            month=int(ed.month),
        )
        out.append(w)

    # Most-recent-first; the simulator and UI both expect reverse-chron.
    out.sort(key=lambda w: w.earn_date_hist, reverse=True)
    return out
