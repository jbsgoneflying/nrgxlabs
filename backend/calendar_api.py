from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.benzinga_client import BenzingaClient
from backend.fmp_client import FmpClient, FmpError
from backend.orats_client import OratsClient
from backend.earnings_calendar import infer_timing_from_time_str
from backend.market_calendar import market_structure_events_by_date, opex_events_by_date
from backend.macro_events import macro_events_by_date
from backend.universe import load_universe_sp500_and_nasdaq100
from backend.earnings_logic import classify_timing
from backend.redis_store import RedisStore
from backend.calendar_snapshot import load_earnings_snapshot

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


@dataclass(frozen=True)
class Engine1UniversePolicy:
    # Kept for backwards compatibility; no longer used by the calendar snapshot path.
    min_price: float = 50.0
    min_market_cap: float = 10_000_000_000.0
    min_avg_dollar_vol_20d: float = 200_000_000.0


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:
            return None
        return f
    except Exception:
        return None


def _pick_first_float(row: dict, keys: List[str]) -> Optional[float]:
    for k in keys:
        v = _to_float(row.get(k))
        if v is not None:
            return v
    return None


def engine1_is_eligible_from_orats_row(row: dict, *, policy: Engine1UniversePolicy) -> Tuple[bool, Dict[str, Any]]:
    """
    Determine Engine-1 eligibility using ORATS /cores snapshot fields.

    We use a flexible set of key names to stay robust to field naming differences
    across ORATS plans / versions.
    """
    px = _pick_first_float(row, ["stockPrice", "spotPrice", "underlyingPrice", "price", "close"])
    mcap = _pick_first_float(row, ["marketCap", "mktCap", "marketcap", "market_cap"])

    avg_vol = _pick_first_float(row, ["avgVol20", "avgVolume20", "avgVol", "avgVolume", "volumeAvg20"])
    avg_dvol = _pick_first_float(row, ["avgDollarVol20", "avgDolVol20", "avgDVol20", "avgDollarVolume20", "dollarVol20"])
    if avg_dvol is None and avg_vol is not None and px is not None and avg_vol > 0 and px > 0:
        avg_dvol = avg_vol * px

    ok_price = (px is not None) and (px >= float(policy.min_price))
    ok_mcap = (mcap is not None) and (mcap >= float(policy.min_market_cap))
    ok_dvol = (avg_dvol is not None) and (avg_dvol >= float(policy.min_avg_dollar_vol_20d))

    # Conservative-but-practical behavior:
    # - Always enforce price minimum.
    # - If we have mcap or dvol, require at least one to clear.
    # - If we have neither, allow based on price alone (keeps the calendar usable even if fields are missing).
    if not ok_price:
        return False, {"price": px, "marketCap": mcap, "avgDollarVol20d": avg_dvol, "reason": "price_below_min"}

    if mcap is None and avg_dvol is None:
        return True, {"price": px, "marketCap": None, "avgDollarVol20d": None, "reason": "price_only_missing_cap_liq"}

    if ok_mcap or ok_dvol:
        return True, {"price": px, "marketCap": mcap, "avgDollarVol20d": avg_dvol, "reason": "pass"}

    return False, {"price": px, "marketCap": mcap, "avgDollarVol20d": avg_dvol, "reason": "cap_liq_below_min"}


def fetch_engine1_eligibility(
    client: OratsClient,
    *,
    ticker: str,
    policy: Engine1UniversePolicy,
) -> Tuple[bool, Dict[str, Any]]:
    fields = ",".join(
        [
            "ticker",
            "stockPrice",
            "spotPrice",
            "marketCap",
            "avgDollarVol20",
            "avgVol20",
        ]
    )
    try:
        rows = client.cores(ticker=str(ticker).upper(), fields=fields).rows or []
        row = rows[0] if rows else {}
    except Exception as e:
        # Be permissive: the calendar should still show names even if ORATS snapshot is unavailable.
        # We'll label this in diagnostics and keep the name in the calendar.
        return True, {"reason": f"orats_error_allow:{type(e).__name__}"}
    ok, diag = engine1_is_eligible_from_orats_row(row if isinstance(row, dict) else {}, policy=policy)
    return ok, diag


def _fetch_benzinga_earnings_range(
    bz: BenzingaClient,
    *,
    start: dt.date,
    end: dt.date,
    max_pages: int = 250,
    pagesize: int = 1000,
) -> Tuple[List[dict], Dict[str, Any]]:
    rows_all: List[dict] = []
    pages = 0
    truncated = False
    for page in range(int(max_pages)):
        resp = bz.calendar_earnings(
            date_from=_fmt_date(start),
            date_to=_fmt_date(end),
            pagesize=int(pagesize),
            page=int(page),
        )
        batch = resp.rows or []
        pages += 1
        rows_all.extend([r for r in batch if isinstance(r, dict)])
        if len(batch) < int(pagesize):
            break
        if page == int(max_pages) - 1 and len(batch) >= int(pagesize):
            truncated = True
    return rows_all, {"pagesFetched": int(pages), "maxPages": int(max_pages), "pageSize": int(pagesize), "truncated": bool(truncated)}


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
    fmp_client: Optional[FmpClient],
    max_tickers: int,
    redis_store: Optional[RedisStore] = None,
    min_market_cap_b: float = 0.0,
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

    # --- MERGE BOTH FMP AND ORATS DATA SOURCES ---
    # Strategy: Get earnings from both sources and merge for best coverage
    # - FMP provides dates (sometimes timing)
    # - ORATS provides reliable BMO/AMC timing
    
    def _coerce_ticker_fmp(row: dict) -> str:
        for k in ("symbol", "ticker", "Symbol", "Ticker"):
            v = row.get(k)
            if v:
                return str(v).strip().upper()
        return ""

    def _coerce_date_fmp(row: dict) -> Optional[dt.date]:
        for k in ("date", "earningDate", "earningsDate", "reportedDate", "Date"):
            v = row.get(k)
            d = _parse_date(v)
            if d is not None:
                return d
        return None

    def _coerce_timing_fmp(row: dict) -> str:
        for k in ("time", "session", "when", "timing", "timeOfDay"):
            v = row.get(k)
            if v is None:
                continue
            t = classify_timing(v)
            if t in ("AMC", "BMO"):
                return t
            s = str(v).strip().lower()
            # AMC indicators
            if any(x in s for x in ("after", "close", "amc", "post", "pm", "evening")):
                return "AMC"
            # BMO indicators
            if any(x in s for x in ("before", "open", "bmo", "pre", "morning")):
                return "BMO"
            # Time strings like "09:00" or "16:30"
            if ":" in s:
                try:
                    hour = int(s.split(":")[0])
                    if hour < 12:
                        return "BMO"
                    else:
                        return "AMC"
                except:
                    pass
        return "UNK"

    # Master map: (ticker, date) -> timing
    # Will be populated from both sources, ORATS timing takes priority
    merged_earnings: Dict[Tuple[str, str], str] = {}
    orats_count = 0
    fmp_count = 0
    
    # ORATS ticker->timing map (ignores date - for cross-referencing FMP dates)
    # This helps when ORATS has estimated dates that don't match FMP's exact dates
    orats_ticker_timing: Dict[str, str] = {}
    
    # 1. Load ORATS snapshot (has reliable BMO/AMC timing)
    if redis_store is not None:
        try:
            orats_snap = load_earnings_snapshot(redis_store)
            if orats_snap and isinstance(orats_snap, dict):
                orats_by_date = orats_snap.get("byDate") or {}
                debug_counts["snapshotUsed"] = True
                debug_counts["snapshotDate"] = str((orats_snap.get("meta") or {}).get("etDate") or "")[:10]
                
                # Build ticker->timing map (most recent timing wins)
                for date_str, timing_groups in orats_by_date.items():
                    if isinstance(timing_groups, dict):
                        for timing_key in ("BMO", "AMC"):  # Only BMO/AMC, skip UNK
                            for ticker in (timing_groups.get(timing_key) or []):
                                if isinstance(ticker, str) and ticker:
                                    sym = ticker.upper()
                                    orats_ticker_timing[sym] = timing_key
                
                # Also add exact-date matches to merged_earnings
                for date_str, timing_groups in orats_by_date.items():
                    if date_str not in day_keys:
                        continue
                    if isinstance(timing_groups, dict):
                        for timing_key in ("BMO", "AMC", "UNK"):
                            for ticker in (timing_groups.get(timing_key) or []):
                                if isinstance(ticker, str) and ticker:
                                    sym = _normalize_symbol_to_universe(ticker.upper())
                                    if universe and sym not in universe:
                                        continue
                                    key = (sym, date_str)
                                    if key not in merged_earnings:
                                        merged_earnings[key] = timing_key
                                        orats_count += 1
                
                debug_counts["oratsTimingMapSize"] = len(orats_ticker_timing)
                LOG.info(f"Calendar: ORATS snapshot loaded, timing map has {len(orats_ticker_timing)} tickers")
            else:
                debug_counts["snapshotUsed"] = False
                notes.append("ORATS snapshot empty or missing - timing data may be limited")
        except Exception as e:
            debug_counts["snapshotUsed"] = False
            LOG.warning(f"Calendar: failed to load ORATS snapshot: {e}")
            notes.append(f"ORATS snapshot error: {type(e).__name__}")
    else:
        debug_counts["snapshotUsed"] = False
        notes.append("Redis not configured - ORATS timing cross-reference unavailable")

    # 2. Add Benzinga earnings data (better forward-looking data than FMP)
    benzinga_count = 0
    benzinga_timing_from_orats = 0  # Count of Benzinga tickers that got timing from ORATS
    if benzinga_client is not None:
        try:
            # Benzinga has good forward-looking earnings data
            bz_resp = benzinga_client.calendar_earnings(
                date_from=_fmt_date(start),
                date_to=_fmt_date(end),
                pagesize=1000,
            )
            bz_rows = bz_resp.rows or []
            debug_counts["benzingaRowsFetched"] = len(bz_rows)
            
            for r in bz_rows:
                if not isinstance(r, dict):
                    continue
                # Benzinga uses 'ticker' field
                sym0 = str(r.get("ticker") or r.get("symbol") or "").strip().upper()
                sym = _normalize_symbol_to_universe(sym0)
                # Benzinga uses 'date' or 'date_confirmed'
                d_str_raw = r.get("date") or r.get("date_confirmed") or ""
                d = _parse_date(d_str_raw)
                if not sym or d is None:
                    continue
                if universe and sym not in universe:
                    continue
                
                d_str = _fmt_date(d)
                if d_str not in day_keys:
                    continue
                
                # Benzinga timing - check multiple fields
                bz_timing = "UNK"
                for time_field in ("time", "timing", "period", "call_time", "report_time"):
                    raw = r.get(time_field)
                    if not raw:
                        continue
                    bz_time = str(raw).strip().lower()
                    if not bz_time:
                        continue
                    
                    # Check for AMC indicators
                    if any(x in bz_time for x in ("amc", "after", "post", "close", "pm", "evening")):
                        bz_timing = "AMC"
                        break
                    # Check for BMO indicators  
                    if any(x in bz_time for x in ("bmo", "before", "pre", "morning")):
                        bz_timing = "BMO"
                        break
                    # Handle exact matches
                    if bz_time in ("am",):
                        bz_timing = "BMO"
                        break
                    # Try to parse time strings like "09:00" or "16:30"
                    if ":" in bz_time:
                        try:
                            hour = int(bz_time.split(":")[0])
                            if hour < 12:
                                bz_timing = "BMO"
                            else:
                                bz_timing = "AMC"
                            break
                        except:
                            pass
                
                # If timing is UNK, check ORATS cross-reference
                if bz_timing == "UNK" and sym in orats_ticker_timing:
                    bz_timing = orats_ticker_timing[sym]
                    benzinga_timing_from_orats += 1
                
                key = (sym, d_str)
                if key not in merged_earnings:
                    merged_earnings[key] = bz_timing
                    benzinga_count += 1
                elif bz_timing != "UNK" and merged_earnings[key] == "UNK":
                    # Upgrade UNK to BMO/AMC if Benzinga has better timing
                    merged_earnings[key] = bz_timing
            
            debug_counts["tickersFromBenzinga"] = benzinga_count
            debug_counts["benzingaTimingFromOrats"] = benzinga_timing_from_orats
        except Exception as e:
            LOG.warning(f"Calendar: Benzinga earnings fetch failed: {e}")
            notes.append(f"Benzinga earnings failed: {type(e).__name__}")

    # 3. Add FMP data (fills gaps, ORATS timing takes priority if conflict)
    fmp_timing_from_orats = 0  # Count of FMP tickers that got timing from ORATS
    if fmp_client is not None:
        try:
            resp = fmp_client.earnings_calendar(date_from=_fmt_date(start), date_to=_fmt_date(end), limit=int(max_tickers))
            rows = resp.rows or []
            debug_counts["fmpRowsFetched"] = int(len(rows))
            
            dropped = 0
            for r in rows:
                if not isinstance(r, dict):
                    continue
                sym0 = _coerce_ticker_fmp(r)
                sym = _normalize_symbol_to_universe(sym0)
                d = _coerce_date_fmp(r)
                if not sym or d is None:
                    continue
                if universe and sym not in universe:
                    dropped += 1
                    continue
                
                d_str = _fmt_date(d)
                if d_str not in day_keys:
                    continue
                    
                key = (sym, d_str)
                if key not in merged_earnings:
                    # New ticker from FMP - check ORATS timing map first
                    fmp_timing = _coerce_timing_fmp(r)
                    final_timing = fmp_timing
                    
                    # If FMP timing is UNK, try to get timing from ORATS (by ticker only)
                    if fmp_timing == "UNK" and sym in orats_ticker_timing:
                        final_timing = orats_ticker_timing[sym]
                        fmp_timing_from_orats += 1
                    
                    merged_earnings[key] = final_timing
                    fmp_count += 1
                # If already in merged_earnings from ORATS/Benzinga, keep existing timing
            
            debug_counts["fmpRowsDroppedUniverse"] = int(dropped)
            debug_counts["fmpTimingFromOrats"] = fmp_timing_from_orats
        except FmpError as e:
            notes.append(f"FMP earnings fetch failed: {str(e)[:180]}")
        except Exception as e:
            notes.append(f"FMP earnings fetch failed: {type(e).__name__}: {str(e)[:180]}")
    else:
        notes.append("FMP unavailable or disabled.")

    # 3. Apply market cap filter if specified
    filtered_earnings = merged_earnings
    mcap_filtered_count = 0
    
    if min_market_cap > 0 and fmp_client is not None:
        # Get all unique tickers
        all_tickers = list(set(sym for sym, d0 in merged_earnings.keys()))
        
        # Fetch market caps from FMP
        try:
            market_caps = fmp_client.get_market_caps(all_tickers)
            debug_counts["marketCapsLoaded"] = len(market_caps)
            
            # Filter by market cap
            filtered_earnings = {}
            for (sym, d0), timing in merged_earnings.items():
                mcap = market_caps.get(sym, 0)
                if mcap >= min_market_cap:
                    filtered_earnings[(sym, d0)] = timing
                else:
                    mcap_filtered_count += 1
            
            debug_counts["filteredByMarketCap"] = mcap_filtered_count
            debug_counts["minMarketCapB"] = min_market_cap_b
        except Exception as e:
            LOG.warning(f"Calendar: market cap filter failed, showing all: {e}")
            notes.append(f"Market cap filter failed: {type(e).__name__}")

    # 4. Populate earnings_by_date from filtered data
    tickers_in_range = 0
    for (sym, d0), timing in filtered_earnings.items():
        earnings_by_date[d0][timing].append({"ticker": sym, "time": ""})
        tickers_in_range += 1

    # Count timing breakdown
    timing_counts = {"BMO": 0, "AMC": 0, "UNK": 0}
    for timing in filtered_earnings.values():
        timing_counts[timing] = timing_counts.get(timing, 0) + 1
    
    debug_counts["earningsSource"] = "merged_orats_benzinga_fmp"
    debug_counts["tickersFromOrats"] = orats_count
    debug_counts["tickersFromBenzinga"] = benzinga_count
    debug_counts["tickersFromFmp"] = fmp_count
    debug_counts["tickersInRange"] = tickers_in_range
    debug_counts["earningsRowsUsed"] = tickers_in_range
    debug_counts["timingBreakdown"] = timing_counts
    LOG.info(f"Calendar: merged {tickers_in_range} earnings (ORATS: {orats_count}, Benzinga: {benzinga_count}, FMP: {fmp_count}, filtered: {mcap_filtered_count}, timing: {timing_counts})")

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


