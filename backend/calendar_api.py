from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.benzinga_client import BenzingaClient
from backend.api_ninjas_client import ApiNinjasClient, ApiNinjasError
from backend.market_calendar import market_structure_events_by_date, opex_events_by_date
from backend.macro_events import macro_events_by_date
from backend.universe import load_universe_sp500_and_nasdaq100

LOG = logging.getLogger(__name__)


def _parse_date(s: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _fmt_date(d: dt.date) -> str:
    return d.isoformat()


def _clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        x = int(float(v))
        return max(int(lo), min(int(hi), x))
    except Exception:
        return int(default)


def _first_of_month(d: dt.date) -> dt.date:
    return dt.date(d.year, d.month, 1)


def _last_of_month(d: dt.date) -> dt.date:
    if d.month == 12:
        nxt = dt.date(d.year + 1, 1, 1)
    else:
        nxt = dt.date(d.year, d.month + 1, 1)
    return nxt - dt.timedelta(days=1)


def _start_of_week_monday(d: dt.date) -> dt.date:
    # Monday=0
    return d - dt.timedelta(days=int(d.weekday()))


def _trading_weekdays(d0: dt.date, d1: dt.date) -> List[dt.date]:
    out: List[dt.date] = []
    cur = d0
    while cur <= d1:
        if cur.weekday() <= 4:  # Mon..Fri
            out.append(cur)
        cur += dt.timedelta(days=1)
    return out


def _build_day_skeleton(view: str, *, start: dt.date, end: dt.date) -> List[dt.date]:
    v = str(view or "").lower().strip()
    if v == "day":
        return [start]
    if v == "week":
        # trading week only (Mon..Fri)
        return _trading_weekdays(start, end)
    # month: all days in month (including weekends to match a full calendar grid)
    out: List[dt.date] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += dt.timedelta(days=1)
    return out


def _event_sort_key(ev: dict) -> Tuple[int, str]:
    kind = str(ev.get("kind") or "").upper()
    # priority: holidays/early close first for planning, then Fed/Econ/Treasury, then OPEX
    pri = 50
    if kind == "HOLIDAY":
        pri = 10
    elif kind == "EARLY_CLOSE":
        pri = 12
    elif kind == "FED":
        pri = 20
    elif kind == "ECON":
        pri = 22
    elif kind == "TREASURY":
        pri = 24
    elif kind == "OPEX":
        pri = 30
    return pri, str(ev.get("title") or "")


def build_calendar_payload(
    *,
    view: str,
    anchor: str,
    tz: str,
    engine1_only: bool,
    include_events: bool,
    benzinga_client: Optional[BenzingaClient],
    max_tickers: int,
    min_market_cap_b: float = 0.0,
    api_ninjas_client: Optional[ApiNinjasClient] = None,
) -> Dict[str, Any]:
    v = str(view or "month").lower().strip()
    if v not in ("month", "week", "day"):
        raise ValueError("Unsupported view. Allowed: month|week|day")
    
    # Convert billions to raw number for filtering
    min_market_cap = float(min_market_cap_b) * 1_000_000_000 if min_market_cap_b > 0 else 0.0

    a = _parse_date(anchor) or dt.date.today()
    if v == "month":
        m0 = _first_of_month(a)
        m1 = _last_of_month(a)
        start = _start_of_week_monday(m0)
        end = m1 + dt.timedelta(days=int(6 - m1.weekday()))
    elif v == "week":
        start = _start_of_week_monday(a)
        end = start + dt.timedelta(days=4)  # Mon..Fri (trading week)
    else:
        start = a
        end = a

    # Build list of calendar days we will return.
    days = _build_day_skeleton(v, start=start, end=end)
    day_keys = {_fmt_date(d) for d in days}

    # Precompute events (market structure + macro) once for the requested range.
    events_by_date: Dict[str, List[dict]] = {k: [] for k in day_keys}
    sources: List[str] = []
    notes: List[str] = []

    if include_events:
        # market structure (holidays + early close) is deterministic
        ms = market_structure_events_by_date(start=start, end=end)
        for d0, evs in ms.items():
            if d0 in events_by_date:
                events_by_date[d0].extend(evs)
        ox = opex_events_by_date(start=start, end=end)
        for d0, evs in ox.items():
            if d0 in events_by_date:
                events_by_date[d0].extend(evs)

        # macro events (Benzinga economics)
        if benzinga_client is not None:
            mx, mx_sources, mx_notes = macro_events_by_date(bz=benzinga_client, start=start, end=end)
            sources.extend(mx_sources)
            notes.extend(mx_notes)
            for d0, evs in mx.items():
                if d0 in events_by_date:
                    events_by_date[d0].extend(evs)
        else:
            notes.append("Benzinga unavailable or disabled; macro events omitted.")

    for k in list(events_by_date.keys()):
        events_by_date[k] = sorted([e for e in (events_by_date.get(k) or []) if isinstance(e, dict)], key=_event_sort_key)

    # Earnings - Try ORATS snapshot first, fall back to FMP live
    earnings_by_date: Dict[str, Dict[str, List[dict]]] = {k: {"BMO": [], "AMC": [], "UNK": []} for k in day_keys}
    debug_counts: Dict[str, Any] = {
        "earningsSource": None,
        "earningsRangeFrom": _fmt_date(start),
        "earningsRangeTo": _fmt_date(end),
        "universeMode": "sp500_nasdaq100",
        "universeSize": None,
        "earningsRowsFetched": 0,
        "earningsRowsUsed": 0,
        "earningsRowsDroppedUniverse": 0,
        "tickersInRange": 0,
        "snapshotUsed": False,
        "snapshotDate": None,
    }

    # Universe filter (default: sp500+nasdaq100). Use env override if you want "all".
    universe_mode = str(os.getenv("FMP_EARNINGS_UNIVERSE") or "sp500_nasdaq100").strip().lower()
    if not universe_mode:
        universe_mode = "sp500_nasdaq100"
    debug_counts["universeMode"] = universe_mode
    universe: Set[str] = set()
    if universe_mode != "all":
        universe = set(load_universe_sp500_and_nasdaq100())
    debug_counts["universeSize"] = int(len(universe)) if universe else None

    def _normalize_symbol_to_universe(sym: str) -> str:
        s = str(sym or "").strip().upper()
        if not s or not universe:
            return s
        if s in universe:
            return s
        if "-" in s:
            s2 = s.replace("-", ".")
            if s2 in universe:
                return s2
        if "." in s:
            s2 = s.replace(".", "-")
            if s2 in universe:
                return s2
        return s

    # --- EARNINGS DATA: API NINJAS PRIMARY SOURCE ---
    # Strategy: Use API Ninjas Premium as primary source (has reliable BMO/AMC timing)
    # Fall back to legacy sources if API Ninjas unavailable
    
    def _coerce_timing_ninja(row: dict) -> str:
        """Convert API Ninjas earnings_timing to BMO/AMC/UNK."""
        # Check multiple possible field names for timing
        # upcomingearnings uses 'earnings_timing', earningscalendar also uses 'earnings_timing'
        timing_raw = None
        for field in ("earnings_timing", "earningsTiming", "time", "timing", "session", "when", "Time"):
            v = row.get(field)
            if v is not None and str(v).strip():
                timing_raw = str(v).strip().lower()
                LOG.debug(f"Found timing in field '{field}': {timing_raw}")
                break
        
        if not timing_raw:
            # Log the row keys for debugging
            LOG.debug(f"No timing field found in row keys: {list(row.keys())}")
            return "UNK"
        
        # Parse timing value - API Ninjas uses these exact values:
        # - before_market: Earnings call occurs before the market opens
        # - during_market: Earnings call occurs during regular market hours
        # - after_market: Earnings call occurs after the market closes
        if timing_raw in ("before_market", "before market", "bmo", "pre", "premarket", "before", "pre-market"):
            return "BMO"
        elif timing_raw in ("after_market", "after market", "during_market", "during market", "amc", "post", "postmarket", "after", "post-market"):
            return "AMC"
        
        # Check for partial matches
        if "before" in timing_raw or "pre" in timing_raw or "bmo" in timing_raw:
            return "BMO"
        if "after" in timing_raw or "post" in timing_raw or "amc" in timing_raw or "during" in timing_raw:
            return "AMC"
        
        LOG.debug(f"Unrecognized timing value: {timing_raw}")
        return "UNK"

    def _coerce_ticker_ninja(row: dict) -> str:
        """Extract ticker from API Ninjas row."""
        for k in ("ticker", "symbol", "Ticker", "Symbol"):
            v = row.get(k)
            if v:
                return str(v).strip().upper()
        return ""

    def _coerce_date_ninja(row: dict) -> Optional[dt.date]:
        """Extract date from API Ninjas row."""
        for k in ("date", "Date"):
            v = row.get(k)
            d = _parse_date(v)
            if d is not None:
                return d
        return None

    # Master map: (ticker, date) -> timing
    merged_earnings: Dict[Tuple[str, str], str] = {}
    ninja_count = 0
    
    # 1. PRIMARY: Try API Ninjas (Premium has reliable BMO/AMC timing)
    use_api_ninjas = api_ninjas_client is not None
    
    if use_api_ninjas:
        try:
            # Fetch all upcoming earnings in the date range
            ninja_rows = api_ninjas_client.fetch_all_upcoming_earnings(
                start_date=_fmt_date(start),
                end_date=_fmt_date(end),
                max_results=int(max_tickers),
            )
            debug_counts["apiNinjasRowsFetched"] = len(ninja_rows)
            
            # Log sample row fields for debugging
            if ninja_rows:
                sample = ninja_rows[0]
                debug_counts["apiNinjaSampleFields"] = list(sample.keys())
                debug_counts["apiNinjaSampleTimingField"] = sample.get("earnings_timing", "NOT_PRESENT")
            
            dropped = 0
            ninja_timing_breakdown = {"BMO": 0, "AMC": 0, "UNK": 0}
            
            for r in ninja_rows:
                if not isinstance(r, dict):
                    continue
                sym0 = _coerce_ticker_ninja(r)
                sym = _normalize_symbol_to_universe(sym0)
                d = _coerce_date_ninja(r)
                if not sym or d is None:
                    continue
                if universe and sym not in universe:
                    dropped += 1
                    continue
                
                d_str = _fmt_date(d)
                if d_str not in day_keys:
                    continue
                
                timing = _coerce_timing_ninja(r)
                ninja_timing_breakdown[timing] = ninja_timing_breakdown.get(timing, 0) + 1
                
                key = (sym, d_str)
                if key not in merged_earnings:
                    merged_earnings[key] = timing
                    ninja_count += 1
            
            debug_counts["apiNinjasRowsDroppedUniverse"] = dropped
            debug_counts["tickersFromApiNinjas"] = ninja_count
            debug_counts["apiNinjasTimingBreakdown"] = ninja_timing_breakdown
            debug_counts["earningsSource"] = "api_ninjas"
            LOG.info(f"Calendar: API Ninjas returned {ninja_count} earnings (timing: {ninja_timing_breakdown})")
            
        except ApiNinjasError as e:
            LOG.warning(f"Calendar: API Ninjas fetch failed, falling back: {e}")
            notes.append(f"API Ninjas failed: {type(e).__name__}; using fallback sources")
            use_api_ninjas = False
        except Exception as e:
            LOG.warning(f"Calendar: API Ninjas unexpected error, falling back: {e}")
            notes.append(f"API Ninjas error: {type(e).__name__}; using fallback sources")
            use_api_ninjas = False
    else:
        notes.append("API Ninjas not configured - no earnings data available")
    
    # 2. Apply market cap filter if specified
    filtered_earnings = merged_earnings
    mcap_filtered_count = 0
    
    debug_counts["minMarketCapB"] = min_market_cap_b
    debug_counts["minMarketCapRaw"] = min_market_cap
    debug_counts["apiNinjasClientAvailable"] = api_ninjas_client is not None
    debug_counts["tickersBeforeFilter"] = len(merged_earnings)
    
    if min_market_cap > 0:
        all_tickers = list(set(sym for sym, d0 in merged_earnings.keys()))
        LOG.info(f"Calendar: Applying market cap filter (min=${min_market_cap/1e9:.1f}B) to {len(all_tickers)} tickers")
        
        market_caps: Dict[str, float] = {}
        
        # Use API Ninjas for market caps (parallel fetching)
        if api_ninjas_client is not None:
            try:
                LOG.info(f"Calendar: Using API Ninjas for market cap filtering")
                market_caps = api_ninjas_client.get_market_caps_batch(all_tickers)
                debug_counts["marketCapSource"] = "api_ninjas"
            except Exception as e:
                LOG.warning(f"Calendar: API Ninjas market cap failed: {e}")
        
        debug_counts["marketCapsLoaded"] = len(market_caps)
        
        # Log some sample market caps for debugging
        if market_caps:
            sample_caps = {k: f"${v/1e9:.1f}B" for k, v in list(market_caps.items())[:5]}
            debug_counts["sampleMarketCaps"] = sample_caps
        
        # If we couldn't load any market caps, skip the filter
        if len(market_caps) == 0:
            LOG.warning(f"Calendar: No market caps loaded from any source, skipping filter")
            notes.append("Market cap filter skipped - no market cap data available")
            debug_counts["marketCapFilterSkipped"] = True
        else:
            filtered_earnings = {}
            for (sym, d0), timing in merged_earnings.items():
                mcap = market_caps.get(sym, 0)
                if mcap >= min_market_cap:
                    filtered_earnings[(sym, d0)] = timing
                else:
                    mcap_filtered_count += 1
            debug_counts["filteredByMarketCap"] = mcap_filtered_count
            debug_counts["tickersAfterFilter"] = len(filtered_earnings)
            LOG.info(f"Calendar: Market cap filter kept {len(filtered_earnings)}/{len(merged_earnings)} tickers (filtered out {mcap_filtered_count})")
            
            # If filter resulted in 0 tickers but we had data, something is wrong - show all
            if len(filtered_earnings) == 0 and len(merged_earnings) > 0:
                LOG.warning(f"Calendar: Market cap filter removed ALL tickers - showing unfiltered results")
                notes.append("Market cap filter returned 0 results - showing all earnings")
                filtered_earnings = merged_earnings
                debug_counts["marketCapFilterOverridden"] = True

    # 4. Populate earnings_by_date from filtered data
    tickers_in_range = 0
    for (sym, d0), timing in filtered_earnings.items():
        earnings_by_date[d0][timing].append({"ticker": sym, "time": ""})
        tickers_in_range += 1

    # Count timing breakdown
    timing_counts = {"BMO": 0, "AMC": 0, "UNK": 0}
    for timing in filtered_earnings.values():
        timing_counts[timing] = timing_counts.get(timing, 0) + 1
    
    debug_counts["tickersInRange"] = tickers_in_range
    debug_counts["earningsRowsUsed"] = tickers_in_range
    debug_counts["timingBreakdown"] = timing_counts
    LOG.info(f"Calendar: {tickers_in_range} earnings from API Ninjas (filtered: {mcap_filtered_count}, timing: {timing_counts})")

    # Stable sort per day by ticker.
    for d0 in earnings_by_date.keys():
        for k in ("BMO", "AMC", "UNK"):
            earnings_by_date[d0][k] = sorted(earnings_by_date[d0][k], key=lambda x: str(x.get("ticker") or ""))

    out_days: List[Dict[str, Any]] = []
    for d in days:
        k = _fmt_date(d)
        out_days.append(
            {
                "date": k,
                "events": events_by_date.get(k) or [],
                "earnings": earnings_by_date.get(k) or {"BMO": [], "AMC": [], "UNK": []},
            }
        )

    return {
        "view": v,
        "tz": str(tz or "America/New_York"),
        "anchor": str(anchor)[:10],
        "range": {"start": _fmt_date(start), "end": _fmt_date(end)},
        "days": out_days,
        "meta": {
            "generatedAt": _fmt_date(dt.date.today()),
            "engine1Only": bool(engine1_only),
            "sourcesUsed": sorted(list(dict.fromkeys([*sources]))),
            "counts": debug_counts,
            "notes": notes,
        },
    }


