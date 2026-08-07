"""
Engine 4: Ichimoku Cloud Continuation Universe Scanner

Scans the SP500 + Nasdaq100 universe for Ichimoku continuation setups
with caching and parallel processing. All market data (daily bars and
live quotes) is served by EODHD via the shared PriceService.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from cachetools import TTLCache

from backend.config import get_flags
from backend.engine4_ichimoku import (
    APLUS_THRESHOLD,
    BETA_LOOKBACK_DEFAULT,
    PLAYBOOK_KIJUN_PULLBACK,
    PLAYBOOK_KUMO_BREAKOUT,
    PLAYBOOK_LABELS,
    PLAYBOOK_TK_CROSS,
    RESEARCH_PLAYBOOKS,
    RS_LOOKBACK_DEFAULT,
    IchimokuSignal,
    build_ichimoku_signal,
    compute_beta_corr,
    compute_detection_context,
    compute_index_ichimoku_state,
    compute_relative_strength,
    detect_ichimoku_setup,
    detect_kumo_breakout_setup,
    detect_tk_cross_setup,
    signal_to_dict,
)
from backend.technicals import (
    DailyBar,
    fetch_daily_bars_range,
    fetch_live_price_context_optional,
)
from backend.universe import load_universe_sp500_and_nasdaq100


LOG = logging.getLogger("engine4_screener")


# ---------------------------------------------------------------------------
# Index Membership Data
# ---------------------------------------------------------------------------

def _read_index_file(path: Path) -> Set[str]:
    """Read tickers from an index file."""
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8", errors="ignore")
    tickers = set()
    for line in text.splitlines():
        s = line.strip().upper()
        if s and not s.startswith("#"):
            tickers.add(s)
    return tickers


def load_index_memberships(repo_root: Optional[Path] = None) -> Dict[str, str]:
    """
    Load index membership for each ticker.
    
    Returns:
        Dict mapping ticker -> "sp500", "nasdaq100", or "both"
    """
    root = repo_root or Path(__file__).resolve().parent.parent
    base = root / "data" / "universe"
    
    sp500 = _read_index_file(base / "sp500.txt")
    nasdaq100 = _read_index_file(base / "nasdaq100.txt")
    
    memberships: Dict[str, str] = {}
    all_tickers = sp500 | nasdaq100
    
    for ticker in all_tickers:
        in_sp = ticker in sp500
        in_ndx = ticker in nasdaq100
        
        if in_sp and in_ndx:
            memberships[ticker] = "both"
        elif in_sp:
            memberships[ticker] = "sp500"
        else:
            memberships[ticker] = "nasdaq100"
    
    return memberships


# ---------------------------------------------------------------------------
# Top-Down Context: index proxies + sector map
# ---------------------------------------------------------------------------

# Liquid ETF proxies used for the index-trend read, relative strength, and
# beta. Nasdaq100 names key off QQQ; everything else off SPY.
INDEX_PROXY = {"sp500": "SPY", "nasdaq100": "QQQ", "both": "QQQ"}


def index_proxy_for(membership: str) -> str:
    return INDEX_PROXY.get(membership, "SPY")


_sector_map_cache: Optional[Dict[str, str]] = None


def load_sector_map(repo_root: Optional[Path] = None) -> Dict[str, str]:
    """Load the ticker -> GICS sector ETF map (data/universe/sector_map.json).

    Names not present in the map simply get no sector context (the scorer
    treats unknown sector as neutral, never a penalty), so a partial map
    degrades gracefully.
    """
    global _sector_map_cache
    if _sector_map_cache is not None:
        return _sector_map_cache
    root = repo_root or Path(__file__).resolve().parent.parent
    # Candidate locations: the repo/volume path first, then the baked-in image
    # seed copy (which is never shadowed by the persistent /app/data volume).
    candidates = [
        root / "data" / "universe" / "sector_map.json",
        Path("/app/seed-data/universe/sector_map.json"),
    ]
    mapping: Dict[str, str] = {}
    for path in candidates:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
                raw = data.get("map", data) if isinstance(data, dict) else {}
                for k, v in raw.items():
                    if isinstance(k, str) and isinstance(v, str):
                        mapping[k.strip().upper()] = v.strip().upper()
                if mapping:
                    break
        except Exception as e:
            LOG.warning(f"Failed to load sector map from {path}: {e}")
    _sector_map_cache = mapping
    return mapping


def fetch_index_context(
    *,
    symbol: str,
    as_of_date: dt.date,
    use_cache: bool = True,
) -> Tuple[List[DailyBar], Dict[str, Any]]:
    """Fetch an index/sector proxy's bars and reduce to a one-line Ichimoku state.

    Returns (bars, state); bars are reused for relative-strength and beta so we
    only pull each proxy once per scan.
    """
    try:
        bars = fetch_bars_for_ticker(
            ticker=symbol, as_of_date=as_of_date, use_cache=use_cache
        )
    except Exception as e:
        LOG.warning(f"Index/sector context fetch failed for {symbol}: {e}")
        bars = []
    state = compute_index_ichimoku_state(bars, symbol=symbol)
    return bars, state


def fetch_sector_states(
    etfs: Set[str],
    *,
    as_of_date: dt.date,
    use_cache: bool = True,
) -> Tuple[Dict[str, List[DailyBar]], Dict[str, Dict[str, Any]]]:
    """Fetch + reduce each distinct sector ETF once. Returns (bars_by_etf, states_by_etf)."""
    bars_by_etf: Dict[str, List[DailyBar]] = {}
    states_by_etf: Dict[str, Dict[str, Any]] = {}
    for etf in sorted(e for e in etfs if e):
        bars, state = fetch_index_context(
            symbol=etf, as_of_date=as_of_date, use_cache=use_cache
        )
        bars_by_etf[etf] = bars
        states_by_etf[etf] = state
    return bars_by_etf, states_by_etf


# ---------------------------------------------------------------------------
# Cache Configuration
# ---------------------------------------------------------------------------

# Full scan cache (structure only; live pricing is overlaid per-request).
# TTL kept short so reloads through the day surface new setups quickly.
try:
    _SCAN_TTL_S = int(get_flags().ENGINE4_CACHE_TTL_SCAN)
except Exception:
    _SCAN_TTL_S = 5 * 60
try:
    _BARS_TTL_S = int(get_flags().ENGINE4_CACHE_TTL_BARS)
except Exception:
    _BARS_TTL_S = 6 * 60 * 60

_scan_cache: TTLCache = TTLCache(maxsize=10, ttl=_SCAN_TTL_S)
_scan_cache_lock = threading.Lock()

# Per-ticker bars cache.
_bars_cache: TTLCache = TTLCache(maxsize=600, ttl=_BARS_TTL_S)
_bars_cache_lock = threading.Lock()

# Signal persistence store (Redis-aware, in-memory fallback). Mirrors the
# Red Dog tracker so E4/E5 lifecycle handling is identical.
_signal_store: Dict[str, Dict[str, Any]] = {}
_signal_store_lock = threading.Lock()

_SIGNAL_TTL_S = 21 * 24 * 3600  # keep tracked signals ~3 weeks
_REDIS_PREFIX = "engine4:signal:"
_REDIS_INDEX = "engine4:signal:index"

# Auto-evaluated lifecycle states (driven by price action).
_TERMINAL_STATUSES = {"target_hit", "stopped", "invalidated", "expired"}
# Desk-managed states (set by the trader, never overwritten by auto-eval).
DESK_STATUSES = {"watching", "entered", "working", "broken", "exited"}


def _signal_key(ticker: str, signal_date: str) -> str:
    return f"{ticker}:{signal_date}"


def _cache_key_scan(as_of: str, min_score: int, direction: Optional[str]) -> str:
    """Generate cache key for full scan results."""
    flags = get_flags()
    flag_hash = hashlib.md5(str(flags.cache_key()).encode()).hexdigest()[:8]
    dir_key = direction or "all"
    return f"e4_scan:{as_of}:{min_score}:{dir_key}:{flag_hash}"


def _cache_key_bars(ticker: str, as_of: str) -> str:
    """Generate cache key for ticker bars."""
    return f"e4_bars:{ticker}:{as_of}"


# ---------------------------------------------------------------------------
# Data Fetching
# ---------------------------------------------------------------------------

def fetch_bars_for_ticker(
    *,
    ticker: str,
    as_of_date: dt.date,
    lookback_days: int = 150,
    use_cache: bool = True,
) -> List[DailyBar]:
    """
    Fetch daily bars for a ticker with caching (EODHD via PriceService).
    Ichimoku needs 52+ bars for Span B, plus 26 bars for cloud projection alignment,
    so we request 150 calendar days to ensure ~100 trading days.

    Set ``use_cache=False`` to force a fresh pull (e.g. the desk hitting "Scan"
    for a current read), bypassing the 6-hour bars cache on read but still
    refreshing it on write.
    """
    as_of_str = as_of_date.isoformat()
    cache_key = _cache_key_bars(ticker, as_of_str)
    
    if use_cache:
        with _bars_cache_lock:
            cached = _bars_cache.get(cache_key)
            if cached is not None:
                return cached
    
    start = as_of_date - dt.timedelta(days=lookback_days)
    bars = fetch_daily_bars_range(ticker=ticker, start=start, end=as_of_date)
    
    with _bars_cache_lock:
        _bars_cache[cache_key] = bars
    
    return bars


def fetch_earnings_days_ahead(
    ticker: str,
    as_of_date: dt.date,
    benzinga_client: Any = None,
) -> Optional[int]:
    """
    Check if earnings are upcoming for a ticker.
    Returns days until earnings, or None if unknown/not soon.
    """
    # Try to use Benzinga client if available
    if benzinga_client is not None:
        try:
            from backend.earnings_calendar import benzinga_next_earnings
            earn_date = benzinga_next_earnings(benzinga_client, ticker=ticker)
            if earn_date:
                earn_dt = dt.date.fromisoformat(str(earn_date)[:10])
                days = (earn_dt - as_of_date).days
                if 0 <= days <= 10:
                    return days
        except Exception:
            pass
    
    return None


# ---------------------------------------------------------------------------
# Single Ticker Scan
# ---------------------------------------------------------------------------

def _compute_dollar_adv(bars: List[DailyBar], lookback: int = 20) -> Optional[float]:
    """20-day average dollar volume (close * volume) — a liquidity floor."""
    window = bars[-lookback:] if len(bars) >= lookback else bars
    vals = []
    for b in window:
        try:
            if b.close and b.volume:
                vals.append(float(b.close) * float(b.volume))
        except (TypeError, ValueError):
            continue
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


# Detectors walked per ticker, in display order. Core first (it stays the
# lead section + the only auto-persisted playbook).
_PLAYBOOK_DETECTORS = (
    (PLAYBOOK_KIJUN_PULLBACK, detect_ichimoku_setup),
    (PLAYBOOK_TK_CROSS, detect_tk_cross_setup),
    (PLAYBOOK_KUMO_BREAKOUT, detect_kumo_breakout_setup),
)


def scan_ticker_playbooks(
    *,
    ticker: str,
    as_of_date: dt.date,
    index_membership: str,
    benzinga_client: Any = None,
    min_dollar_adv: float = 0.0,
    use_cache: bool = True,
    index_bars: Optional[List[DailyBar]] = None,
    index_state: Optional[Dict[str, Any]] = None,
    sector_etf: Optional[str] = None,
    sector_state: Optional[Dict[str, Any]] = None,
    rs_lookback: int = RS_LOOKBACK_DEFAULT,
    beta_lookback: int = BETA_LOOKBACK_DEFAULT,
    bars: Optional[List[DailyBar]] = None,
) -> List[IchimokuSignal]:
    """
    Scan a single ticker across ALL Ichimoku playbooks (core Kijun pullback,
    TK cross, Kumo breakout) on the same cached bars — one bar fetch and one
    Ichimoku-series computation per ticker, no extra EODHD calls.

    Pass ``bars`` to scan a pre-built series (the close preview injects a
    synthetic today-bar from the live quote); otherwise bars are fetched
    with the normal cache behaviour.

    Returns a list of scored IchimokuSignal (zero to three entries, at most
    one per playbook).
    """
    try:
        if bars is None:
            bars = fetch_bars_for_ticker(ticker=ticker, as_of_date=as_of_date, use_cache=use_cache)
        
        if not bars or len(bars) < 60:
            return []
        
        # Item 7: liquidity filter — skip names that can't absorb desk size.
        dollar_adv = _compute_dollar_adv(bars)
        if min_dollar_adv > 0 and (dollar_adv is None or dollar_adv < min_dollar_adv):
            return []
        
        # Check earnings
        earnings_days = fetch_earnings_days_ahead(ticker, as_of_date, benzinga_client)
        
        # Shared detection inputs (Ichimoku series + indicator stack), computed
        # once and handed to every playbook detector.
        context, _err = compute_detection_context(bars)
        if context is None:
            return []
        
        detections = []
        for _playbook, detector in _PLAYBOOK_DETECTORS:
            detection = detector(
                bars,
                ticker=ticker,
                index_membership=index_membership,
                earnings_days_ahead=earnings_days,
                context=context,
            )
            if detection.get("hasSignal"):
                detections.append(detection)
        
        if not detections:
            return []
        
        closes = context["closes"]
        tenkan_series = context["tenkan_series"]

        # Top-down context: relative strength + beta/corr vs the index proxy.
        rs_ratio: Optional[float] = None
        beta: Optional[float] = None
        corr: Optional[float] = None
        if index_bars:
            index_closes = [float(b.close) for b in index_bars if b.close is not None]
            if index_closes and closes:
                rs_ratio = compute_relative_strength(
                    closes, index_closes, lookback=rs_lookback
                ).get("rsRatio")
                bc = compute_beta_corr(closes, index_closes, lookback=beta_lookback)
                beta = bc.get("beta")
                corr = bc.get("corr")
        
        # Build scored signals with freshness classification
        signals: List[IchimokuSignal] = []
        for detection in detections:
            signal = build_ichimoku_signal(
                ticker=ticker,
                detection=detection,
                bars=bars,
                closes=closes,
                tenkan_series=tenkan_series,
                earnings_days_ahead=earnings_days,
                index_membership=index_membership,
                dollar_adv=dollar_adv,
                sector_etf=sector_etf,
                sector_state=sector_state,
                index_state=index_state,
                rs_ratio=rs_ratio,
                beta=beta,
                corr=corr,
            )
            if signal is not None:
                signals.append(signal)
        
        return signals
        
    except Exception as e:
        LOG.warning(f"Error scanning {ticker}: {e}")
        return []


def scan_ticker(
    *,
    ticker: str,
    as_of_date: dt.date,
    index_membership: str,
    benzinga_client: Any = None,
    min_dollar_adv: float = 0.0,
    use_cache: bool = True,
    index_bars: Optional[List[DailyBar]] = None,
    index_state: Optional[Dict[str, Any]] = None,
    sector_etf: Optional[str] = None,
    sector_state: Optional[Dict[str, Any]] = None,
    rs_lookback: int = RS_LOOKBACK_DEFAULT,
    beta_lookback: int = BETA_LOOKBACK_DEFAULT,
) -> Optional[IchimokuSignal]:
    """
    Scan a single ticker for the CORE Ichimoku continuation setup (Kijun
    pullback). Kept for back-compat; the universe scan walks every playbook
    via scan_ticker_playbooks.
    """
    signals = scan_ticker_playbooks(
        ticker=ticker,
        as_of_date=as_of_date,
        index_membership=index_membership,
        benzinga_client=benzinga_client,
        min_dollar_adv=min_dollar_adv,
        use_cache=use_cache,
        index_bars=index_bars,
        index_state=index_state,
        sector_etf=sector_etf,
        sector_state=sector_state,
        rs_lookback=rs_lookback,
        beta_lookback=beta_lookback,
    )
    for signal in signals:
        if signal.playbook == PLAYBOOK_KIJUN_PULLBACK:
            return signal
    return None


def scan_single_ticker(
    *,
    ticker: str,
    as_of_date: Optional[str] = None,
    benzinga_client: Any = None,
) -> Dict[str, Any]:
    """
    Full analysis for a single ticker (for detail endpoint).
    """
    t = str(ticker).strip().upper()
    today = dt.date.today()
    if as_of_date:
        try:
            today = dt.date.fromisoformat(str(as_of_date)[:10])
        except Exception:
            today = dt.date.today()
    
    # Determine index membership
    memberships = load_index_memberships()
    index_membership = memberships.get(t, "sp500")
    
    # Index context for the membership's proxy
    proxy = index_proxy_for(index_membership)
    index_bars, index_state = fetch_index_context(symbol=proxy, as_of_date=today)

    # Sector context for this name
    sector_map = load_sector_map()
    sector_etf = sector_map.get(t)
    sector_state = None
    if sector_etf:
        _, sector_state = fetch_index_context(symbol=sector_etf, as_of_date=today)
    
    # Fetch bars
    bars = fetch_bars_for_ticker(ticker=t, as_of_date=today)
    
    if not bars or len(bars) < 60:
        return {
            "enabled": False,
            "ticker": t,
            "asOfDate": today.isoformat(),
            "notes": ["Insufficient data (need 60+ bars)."],
        }
    
    # Check earnings
    earnings_days = fetch_earnings_days_ahead(t, today, benzinga_client)
    
    # Shared detection inputs for every playbook.
    context, _err = compute_detection_context(bars)
    
    # Full detection (core playbook drives the headline fields)
    detection = detect_ichimoku_setup(
        bars,
        ticker=t,
        index_membership=index_membership,
        earnings_days_ahead=earnings_days,
        context=context,
    )
    
    result = {
        "enabled": detection.get("enabled", False),
        "ticker": t,
        "asOfDate": today.isoformat(),
        "hasSignal": detection.get("hasSignal", False),
        "signal": None,
        "trend": detection.get("trend"),
        "pullback": detection.get("pullback"),
        "trigger": detection.get("trigger"),
        "indicators": detection.get("indicators"),
        "indexState": index_state,
        "sectorState": sector_state,
        "indexMembership": index_membership,
        "earningsDaysAhead": earnings_days,
        "notes": detection.get("notes", []),
        "playbooks": {},
    }
    
    # Top-down context, shared by every playbook's signal build.
    ich_closes = context["closes"] if context else []
    rs_ratio: Optional[float] = None
    beta: Optional[float] = None
    corr: Optional[float] = None
    if index_bars and ich_closes:
        index_closes = [float(b.close) for b in index_bars if b.close is not None]
        if index_closes:
            rs_ratio = compute_relative_strength(ich_closes, index_closes).get("rsRatio")
            bc = compute_beta_corr(ich_closes, index_closes)
            beta = bc.get("beta")
            corr = bc.get("corr")

    def _build(det: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        signal = build_ichimoku_signal(
            ticker=t,
            detection=det,
            bars=bars,
            closes=ich_closes,
            tenkan_series=context["tenkan_series"] if context else [],
            earnings_days_ahead=earnings_days,
            index_membership=index_membership,
            dollar_adv=_compute_dollar_adv(bars),
            sector_etf=sector_etf,
            sector_state=sector_state,
            index_state=index_state,
            rs_ratio=rs_ratio,
            beta=beta,
            corr=corr,
        )
        return signal_to_dict(signal) if signal else None

    if detection.get("hasSignal"):
        result["signal"] = _build(detection)
    
    # Research playbooks: report which fire for this name.
    if context is not None:
        for pb, detector in _PLAYBOOK_DETECTORS:
            if pb == PLAYBOOK_KIJUN_PULLBACK:
                continue
            det = detector(
                bars,
                ticker=t,
                index_membership=index_membership,
                earnings_days_ahead=earnings_days,
                context=context,
            )
            entry: Dict[str, Any] = {
                "label": PLAYBOOK_LABELS.get(pb, pb),
                "research": True,
                "hasSignal": bool(det.get("hasSignal")),
                "signal": None,
                "notes": det.get("notes", []),
            }
            if det.get("hasSignal"):
                entry["signal"] = _build(det)
            result["playbooks"][pb] = entry
    
    return result


# ---------------------------------------------------------------------------
# Full Universe Scan
# ---------------------------------------------------------------------------

def _bucketize_signals(
    signals: List[IchimokuSignal],
    *,
    direction: Optional[str],
    min_rr: float,
    structure_max: int,
) -> Dict[str, Any]:
    """Apply the shared post-scan quality pipeline to one playbook's signals.

    Filter to A+ only (score >= 75) and by direction if specified, enforce
    the risk:reward floor, split into freshness buckets, and trim structure
    to a tight, ranked "Approaching" shortlist (closest-to-actionable first,
    then score) so the desk isn't drowning in names days away from a trigger.
    """
    aplus_signals: List[IchimokuSignal] = []
    sub_rr_count = 0
    for s in signals:
        if s.score < APLUS_THRESHOLD:
            continue
        if direction and s.direction != direction:
            continue
        # Risk:reward floor. reward_1r / risk_dollars must clear min_rr (default
        # 1:1). A setup can be technically perfect yet have Target 1 nearer than
        # the stop, which makes for a losing-expectancy trade the desk shouldn't
        # see. Guard against a zero/None risk so we never divide by zero.
        if min_rr > 0:
            risk = s.risk_dollars or 0.0
            reward = s.reward_1r or 0.0
            if risk <= 0 or (reward / risk) < min_rr:
                sub_rr_count += 1
                continue
        aplus_signals.append(s)

    aplus_signals.sort(key=lambda x: x.score, reverse=True)

    actionable: List[IchimokuSignal] = []
    structure: List[IchimokuSignal] = []
    rejected_count = 0
    for s in aplus_signals:
        if s.freshness_bucket == "actionable":
            actionable.append(s)
        elif s.freshness_bucket == "structure":
            structure.append(s)
        elif s.freshness_bucket == "rejected":
            rejected_count += 1
            # Don't include rejected signals in output

    structure.sort(
        key=lambda x: ((x.distance_to_actionable if x.distance_to_actionable is not None else 999.0), -x.score)
    )
    structure_total = len(structure)
    if structure_max > 0:
        structure = structure[:structure_max]

    return {
        "aplus": aplus_signals,
        "actionable": actionable,
        "structure": structure,
        "structure_total": structure_total,
        "rejected_count": rejected_count,
        "sub_rr_count": sub_rr_count,
    }


def run_universe_scan(
    *,
    as_of_date: Optional[str] = None,
    min_score: int = 50,
    direction: Optional[str] = None,
    benzinga_client: Any = None,
    max_workers: int = 10,
    use_cache: bool = True,
    persist: bool = True,
) -> Dict[str, Any]:
    """
    Scan the full SP500 + Nasdaq100 universe for Ichimoku setups.
    
    Args:
        as_of_date: Scan date (YYYY-MM-DD), defaults to today
        min_score: Minimum score to include (0-100)
        direction: Filter by direction ("bullish", "bearish", or None for both)
        benzinga_client: Optional Benzinga client for earnings check
        max_workers: Number of parallel workers
        use_cache: When False, bypass the scan + bars caches and pull fresh
            (used by the "Scan" button and the cron breadth job).
        persist: When False, do not seed the desk tracker store with scanned
            names (used by the breadth job so it can't pollute the tracker with
            names the desk never looked at).
    
    Returns:
        Dict with scan results and stats
    """
    start_time = time.time()
    
    today = dt.date.today()
    if as_of_date:
        try:
            today = dt.date.fromisoformat(str(as_of_date)[:10])
        except Exception:
            today = dt.date.today()
    
    as_of_str = today.isoformat()
    
    flags = get_flags()
    min_dollar_adv = float(getattr(flags, "ENGINE4_MIN_DOLLAR_ADV", 0.0) or 0.0)
    structure_max = int(getattr(flags, "ENGINE4_STRUCTURE_MAX", 16) or 16)
    min_rr = float(getattr(flags, "ENGINE4_MIN_RR", 1.0) or 0.0)
    
    # Check cache (structure scan only; live pricing is overlaid per-request
    # by the router so a cache hit still reflects the current market).
    cache_key = _cache_key_scan(as_of_str, min_score, direction)
    if use_cache:
        with _scan_cache_lock:
            cached = _scan_cache.get(cache_key)
            if cached is not None:
                return cached
    
    # Load universe and memberships
    universe = load_universe_sp500_and_nasdaq100()
    memberships = load_index_memberships()

    # Top-down context, computed once per scan:
    #  - index Ichimoku state + bars (for the alignment gate, RS, and beta)
    #  - sector ETF states (one Ichimoku read per distinct sector ETF)
    rs_lookback = int(getattr(flags, "ENGINE4_RS_LOOKBACK", RS_LOOKBACK_DEFAULT) or RS_LOOKBACK_DEFAULT)
    beta_lookback = int(getattr(flags, "ENGINE4_BETA_LOOKBACK", BETA_LOOKBACK_DEFAULT) or BETA_LOOKBACK_DEFAULT)
    spy_bars, index_spx = fetch_index_context(
        symbol=index_proxy_for("sp500"), as_of_date=today, use_cache=use_cache
    )
    qqq_bars, index_ndx = fetch_index_context(
        symbol=index_proxy_for("nasdaq100"), as_of_date=today, use_cache=use_cache
    )
    sector_map = load_sector_map()
    needed_etfs = {sector_map[t] for t in universe if sector_map.get(t)}
    _, sector_states = fetch_sector_states(
        needed_etfs, as_of_date=today, use_cache=use_cache
    )
    
    # Scan in parallel — every playbook per ticker on the same cached bars.
    signals: List[IchimokuSignal] = []
    errors: List[str] = []
    
    def _scan_one(ticker: str) -> List[IchimokuSignal]:
        membership = memberships.get(ticker, "sp500")
        
        # Select appropriate index context
        if membership == "nasdaq100":
            idx_bars, idx_state = qqq_bars, index_ndx
        else:
            idx_bars, idx_state = spy_bars, index_spx

        s_etf = sector_map.get(ticker)
        s_state = sector_states.get(s_etf) if s_etf else None
        
        return scan_ticker_playbooks(
            ticker=ticker,
            as_of_date=today,
            index_membership=membership,
            benzinga_client=benzinga_client,
            min_dollar_adv=min_dollar_adv,
            use_cache=use_cache,
            index_bars=idx_bars,
            index_state=idx_state,
            sector_etf=s_etf,
            sector_state=s_state,
            rs_lookback=rs_lookback,
            beta_lookback=beta_lookback,
        )
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ticker = {executor.submit(_scan_one, t): t for t in universe}
        
        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            try:
                signals.extend(future.result())
            except Exception as e:
                errors.append(f"{ticker}: {str(e)}")
    
    # Same quality bar for every playbook: A+ only, direction filter, R:R
    # floor, freshness bucketing, capped + ranked structure shortlist.
    core = _bucketize_signals(
        [s for s in signals if s.playbook == PLAYBOOK_KIJUN_PULLBACK],
        direction=direction, min_rr=min_rr, structure_max=structure_max,
    )
    playbook_blocks: Dict[str, Dict[str, Any]] = {}
    for pb in RESEARCH_PLAYBOOKS:
        b = _bucketize_signals(
            [s for s in signals if s.playbook == pb],
            direction=direction, min_rr=min_rr, structure_max=structure_max,
        )
        playbook_blocks[pb] = {
            "label": PLAYBOOK_LABELS.get(pb, pb),
            "research": True,
            "totalAPlus": len(b["aplus"]),
            "actionableCount": len(b["actionable"]),
            "structureCount": len(b["structure"]),
            "structureTotal": b["structure_total"],
            "rejectedCount": b["rejected_count"],
            "subRRRejected": b["sub_rr_count"],
            "actionable": [signal_to_dict(s) for s in b["actionable"]],
            "structure": [signal_to_dict(s) for s in b["structure"]],
        }
    
    # Persist actionable + structure to the tracker store (Redis-aware,
    # preserves desk overrides). Only fresh actionable names auto-enter as
    # "pending"; structure stays as a watch surface. NOTE: _persist_signals
    # expects API dicts, not IchimokuSignal dataclasses.
    # The cron breadth job passes persist=False so it can't seed the desk
    # tracker with names nobody looked at.
    # Research-first: ONLY the core playbook auto-persists. TK cross / Kumo
    # breakout names enter the tracker via a manual Watch only.
    if persist:
        _persist_signals([signal_to_dict(s) for s in (core["actionable"] + core["structure"])])
    
    elapsed_ms = int((time.time() - start_time) * 1000)
    
    result = {
        "asOfDate": as_of_str,
        "scannedCount": len(universe),
        "totalAPlus": len(core["aplus"]),
        "actionableCount": len(core["actionable"]),
        "structureCount": len(core["structure"]),
        "structureTotal": core["structure_total"],
        "rejectedCount": core["rejected_count"],
        "actionable": [signal_to_dict(s) for s in core["actionable"]],
        "structure": [signal_to_dict(s) for s in core["structure"]],
        "playbooks": playbook_blocks,
        "indexState": {
            "spx": index_spx,
            "ndx": index_ndx,
        },
        "sectorStates": sector_states,
        "meta": {
            "scanDurationMs": elapsed_ms,
            "direction": direction,
            "minDollarAdv": min_dollar_adv,
            "structureMax": structure_max,
            "minRR": min_rr,
            "subRRRejected": core["sub_rr_count"],
            "rsLookback": rs_lookback,
            "betaLookback": beta_lookback,
            "errors": errors[:10] if errors else [],
        },
    }
    
    # Cache result
    with _scan_cache_lock:
        _scan_cache[cache_key] = result
    
    return result


# ---------------------------------------------------------------------------
# Live Re-Pricing Overlay
# ---------------------------------------------------------------------------

def compute_live_state(
    *,
    direction: str,
    price: float,
    entry_trigger: Optional[float],
    stop_loss: Optional[float],
    target_1: Optional[float],
    atr: Optional[float],
) -> Dict[str, Any]:
    """Re-evaluate a setup's *entry trigger* against the current price.

    The Ichimoku structure (Tenkan/Kijun/cloud, and therefore the trigger and
    stop levels) is fixed for the day once the prior bar closes. What moves
    intraday is price relative to those fixed levels — which is exactly what
    tells the desk "is this still 0.29 away, or did it already trigger?".

    ``toTrigger`` is the signed distance the price must still travel to reach
    the entry trigger (positive = not yet triggered). ``state`` is one of:
    pending | triggered | stopped | target1.
    """
    is_bull = direction == "bullish"
    out: Dict[str, Any] = {
        "price": round(float(price), 4),
        "toTrigger": None,
        "toTriggerPct": None,
        "toTriggerAtr": None,
        "state": "pending",
    }

    if entry_trigger is not None and price > 0:
        # Distance the price must still move to hit the trigger.
        to_trigger = (entry_trigger - price) if is_bull else (price - entry_trigger)
        out["toTrigger"] = round(to_trigger, 4)
        out["toTriggerPct"] = round((to_trigger / price) * 100.0, 3)
        if atr and atr > 0:
            out["toTriggerAtr"] = round(to_trigger / atr, 2)

    # A continuation entry is a stop order beyond the trigger level, so the
    # stop/target only mean anything *after* the trigger fires. Before it
    # fires, an adverse move through the stop level means the setup broke down
    # before triggering → "invalidated" (not "stopped").
    triggered = False
    if entry_trigger is not None:
        triggered = (price >= entry_trigger) if is_bull else (price <= entry_trigger)

    state = "pending"
    if triggered:
        state = "triggered"
        if target_1 is not None and ((price >= target_1) if is_bull else (price <= target_1)):
            state = "target1"
        elif stop_loss is not None and ((price <= stop_loss) if is_bull else (price >= stop_loss)):
            state = "stopped"
    else:
        # Not yet triggered: flag a pre-trigger breakdown through the stop.
        if stop_loss is not None and ((price <= stop_loss) if is_bull else (price >= stop_loss)):
            state = "invalidated"
    out["state"] = state
    return out


def overlay_signal_list(
    sigs: List[Dict[str, Any]],
    *,
    max_workers: int = 10,
) -> int:
    """Annotate a flat list of signal dicts with a fresh ``live`` block.

    Each signal must carry ``ticker``, ``direction``, ``levels`` and
    ``indicators``. One live quote per distinct ticker (EODHD). Returns the
    number of signals annotated.
    """
    sigs = [s for s in (sigs or []) if isinstance(s, dict)]
    if not sigs:
        return 0

    # De-dup tickers so a name in multiple buckets is only quoted once.
    by_ticker: Dict[str, List[Dict[str, Any]]] = {}
    for s in sigs:
        t = str(s.get("ticker") or "").upper()
        if t:
            by_ticker.setdefault(t, []).append(s)

    def _quote(ticker: str) -> Optional[Dict[str, Any]]:
        try:
            return fetch_live_price_context_optional(ticker=ticker)
        except Exception:
            return None

    contexts: Dict[str, Optional[Dict[str, Any]]] = {}
    if by_ticker:
        workers = max(1, min(max_workers, len(by_ticker)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_quote, t): t for t in by_ticker}
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    contexts[t] = fut.result()
                except Exception:
                    contexts[t] = None

    annotated = 0
    now_iso = dt.datetime.utcnow().isoformat() + "Z"
    for ticker, members in by_ticker.items():
        ctx = contexts.get(ticker) or {}
        price = ctx.get("price")
        if price is None or float(price) <= 0:
            for s in members:
                s["live"] = {"available": False, "asOf": now_iso}
            continue
        for s in members:
            levels = s.get("levels", {}) or {}
            indicators = s.get("indicators", {}) or {}
            state = compute_live_state(
                direction=s.get("direction", "bullish"),
                price=float(price),
                entry_trigger=levels.get("entryTrigger"),
                stop_loss=levels.get("stopLoss"),
                target_1=levels.get("target1"),
                atr=indicators.get("atr"),
            )
            state["available"] = True
            state["asOf"] = now_iso
            state["marketOpen"] = bool(ctx.get("marketOpen"))
            state["source"] = ctx.get("source")
            s["live"] = state
            annotated += 1

    return annotated


def apply_live_price_overlay(
    result: Dict[str, Any],
    *,
    max_workers: int = 10,
) -> int:
    """Annotate the surfaced scan signals (actionable + structure) in-place
    with a fresh ``live`` block.

    Runs on every request (even a structure-cache hit), so reloading the page
    through the day always re-prices the displayed names against the current
    market without re-running the expensive universe scan.
    """
    if not isinstance(result, dict):
        return 0
    sigs: List[Dict[str, Any]] = []
    for key in ("actionable", "structure"):
        block = result.get(key)
        if isinstance(block, list):
            sigs.extend([s for s in block if isinstance(s, dict)])
    # Research playbook sections re-price the same way (one quote per
    # distinct ticker across all sections — overlay_signal_list de-dups).
    playbooks = result.get("playbooks")
    if isinstance(playbooks, dict):
        for block in playbooks.values():
            if not isinstance(block, dict):
                continue
            for key in ("actionable", "structure"):
                lst = block.get(key)
                if isinstance(lst, list):
                    sigs.extend([s for s in lst if isinstance(s, dict)])
    return overlay_signal_list(sigs, max_workers=max_workers)


def overlay_tracker_signals(
    signals: Dict[str, Any],
    *,
    max_workers: int = 10,
) -> int:
    """Annotate every tracked record (across all lifecycle buckets) with a
    fresh ``live`` block, so the desk book reflects current pricing even on a
    plain (non-refresh) load.
    """
    if not isinstance(signals, dict):
        return 0
    flat: List[Dict[str, Any]] = []
    for v in signals.values():
        if isinstance(v, list):
            flat.extend([s for s in v if isinstance(s, dict)])
    return overlay_signal_list(flat, max_workers=max_workers)


# ---------------------------------------------------------------------------
# Close Preview — "if today's candle closed right now, what would fire?"
# ---------------------------------------------------------------------------
# Daily-candle systems decide at the close, but the desk wants to act in the
# last 15-20 minutes of the session while the candle is still forming. The
# preview synthesizes today's bar from the live EODHD quote (open/high/low/
# last + running volume), appends it to the cached daily series, and runs
# every playbook detector on it — same scoring, same freshness rules, same
# gate. Nothing here persists to the tracker: the preview is a decision
# surface, not a record.

_preview_cache: TTLCache = TTLCache(maxsize=4, ttl=90)
_preview_cache_lock = threading.Lock()


def synthesize_preview_bar(snap: Dict[str, Any], today_str: str) -> Optional[DailyBar]:
    """Build today's forming daily bar from a live quote snapshot.

    The quote's ``close`` is the last trade — the "what if it closed here"
    price. Missing open/high/low fall back to consistent values so the bar
    is always well-formed (high >= open/close >= low).
    """
    close = snap.get("close")
    if close is None or float(close) <= 0:
        return None
    close = float(close)
    o = snap.get("open")
    o = float(o) if (o is not None and float(o) > 0) else close
    highs = [float(v) for v in (snap.get("high"), o, close) if v is not None and float(v) > 0]
    lows = [float(v) for v in (snap.get("low"), o, close) if v is not None and float(v) > 0]
    vol = snap.get("volume")
    return DailyBar(
        trade_date=today_str,
        open=o,
        high=max(highs),
        low=min(lows),
        close=close,
        volume=(float(vol) if vol else None),
        vwap=None,
    )


def bars_with_preview_close(
    bars: List[DailyBar],
    snap: Optional[Dict[str, Any]],
    today_str: str,
) -> Tuple[List[DailyBar], bool]:
    """Append the forming today-bar unless the real EOD row is already in.

    Returns (bars, synthetic) — synthetic is False when today's close is
    already published (post-close) or no usable quote exists, in which case
    the series is returned unchanged and the preview equals the real scan.
    """
    if not bars:
        return bars, False
    if str(bars[-1].trade_date) >= today_str:
        return bars, False
    if not snap:
        return bars, False
    synthetic = synthesize_preview_bar(snap, today_str)
    if synthetic is None:
        return bars, False
    return list(bars) + [synthetic], True


def _preview_index_context(
    symbol: str,
    today: dt.date,
    snaps: Dict[str, Dict[str, Any]],
    today_str: str,
) -> Tuple[List[DailyBar], Dict[str, Any]]:
    """Index/sector proxy state evaluated on the same forming-candle basis."""
    bars, _ = fetch_index_context(symbol=symbol, as_of_date=today)
    pbars, _syn = bars_with_preview_close(bars, snaps.get(symbol), today_str)
    return pbars, compute_index_ichimoku_state(pbars, symbol=symbol)


def run_close_preview(
    *,
    direction: Optional[str] = None,
    benzinga_client: Any = None,
    max_workers: int = 10,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Scan the universe as if today's daily candle closed at the live price.

    Same pipeline as run_universe_scan (all playbooks, A+ bar, R:R floor,
    freshness buckets) but on bars ending with a synthetic today-bar built
    from live EODHD quotes. Results are marked ``preview`` and never persist
    to the desk tracker. Cached for 90s — the desk polls this in the closing
    window.
    """
    from backend.market_hours import is_us_equity_market_open, minutes_to_close
    from backend.price_service import get_price_service

    start_time = time.time()
    today = dt.date.today()
    as_of_str = today.isoformat()

    cache_key = f"e4_preview:{as_of_str}:{direction or 'all'}"
    if use_cache:
        with _preview_cache_lock:
            cached = _preview_cache.get(cache_key)
            if cached is not None:
                return cached

    flags = get_flags()
    min_dollar_adv = float(getattr(flags, "ENGINE4_MIN_DOLLAR_ADV", 0.0) or 0.0)
    structure_max = int(getattr(flags, "ENGINE4_STRUCTURE_MAX", 16) or 16)
    min_rr = float(getattr(flags, "ENGINE4_MIN_RR", 1.0) or 0.0)
    rs_lookback = int(getattr(flags, "ENGINE4_RS_LOOKBACK", RS_LOOKBACK_DEFAULT) or RS_LOOKBACK_DEFAULT)
    beta_lookback = int(getattr(flags, "ENGINE4_BETA_LOOKBACK", BETA_LOOKBACK_DEFAULT) or BETA_LOOKBACK_DEFAULT)

    universe = load_universe_sp500_and_nasdaq100()
    memberships = load_index_memberships()
    sector_map = load_sector_map()
    needed_etfs = sorted({sector_map[t] for t in universe if sector_map.get(t)})
    proxies = [index_proxy_for("sp500"), index_proxy_for("nasdaq100")] + needed_etfs

    ps = get_price_service()
    if ps is None:
        raise RuntimeError("EODHD price service unavailable — cannot build close preview")

    # One batched pass for every live quote the preview needs (universe +
    # index proxies + sector ETFs).
    snaps = ps.fetch_live_bar_snapshots(list(universe) + proxies)
    market_open = is_us_equity_market_open()
    mins_to_close = minutes_to_close()
    now_iso = dt.datetime.utcnow().isoformat() + "Z"

    # Top-down context on the same forming-candle basis.
    spy_bars, index_spx = _preview_index_context(index_proxy_for("sp500"), today, snaps, as_of_str)
    qqq_bars, index_ndx = _preview_index_context(index_proxy_for("nasdaq100"), today, snaps, as_of_str)
    sector_states: Dict[str, Dict[str, Any]] = {}
    for etf in needed_etfs:
        _b, st = _preview_index_context(etf, today, snaps, as_of_str)
        sector_states[etf] = st

    signals: List[IchimokuSignal] = []
    errors: List[str] = []
    synthetic_count = 0

    def _scan_one(ticker: str) -> Tuple[List[IchimokuSignal], bool]:
        snap = snaps.get(ticker)
        if snap is None:
            return [], False
        base = fetch_bars_for_ticker(ticker=ticker, as_of_date=today, use_cache=True)
        if not base or len(base) < 60:
            return [], False
        pbars, synthetic = bars_with_preview_close(base, snap, as_of_str)
        membership = memberships.get(ticker, "sp500")
        if membership == "nasdaq100":
            idx_bars, idx_state = qqq_bars, index_ndx
        else:
            idx_bars, idx_state = spy_bars, index_spx
        s_etf = sector_map.get(ticker)
        sigs = scan_ticker_playbooks(
            ticker=ticker,
            as_of_date=today,
            index_membership=membership,
            benzinga_client=benzinga_client,
            min_dollar_adv=min_dollar_adv,
            index_bars=idx_bars,
            index_state=idx_state,
            sector_etf=s_etf,
            sector_state=sector_states.get(s_etf) if s_etf else None,
            rs_lookback=rs_lookback,
            beta_lookback=beta_lookback,
            bars=pbars,
        )
        return sigs, synthetic

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ticker = {executor.submit(_scan_one, t): t for t in universe}
        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            try:
                sigs, synthetic = future.result()
                signals.extend(sigs)
                if synthetic:
                    synthetic_count += 1
            except Exception as e:
                errors.append(f"{ticker}: {str(e)}")

    def _preview_dicts(sig_list: List[IchimokuSignal]) -> List[Dict[str, Any]]:
        out = []
        for s in sig_list:
            d = signal_to_dict(s)
            d["preview"] = True
            snap = snaps.get(s.ticker) or {}
            price = snap.get("close")
            if price:
                live = compute_live_state(
                    direction=d.get("direction", "bullish"),
                    price=float(price),
                    entry_trigger=(d.get("levels") or {}).get("entryTrigger"),
                    stop_loss=(d.get("levels") or {}).get("stopLoss"),
                    target_1=(d.get("levels") or {}).get("target1"),
                    atr=(d.get("indicators") or {}).get("atr"),
                )
                live.update({
                    "available": True,
                    "asOf": now_iso,
                    "marketOpen": market_open,
                    "source": "eodhd_live_quote",
                })
                d["live"] = live
            out.append(d)
        return out

    core = _bucketize_signals(
        [s for s in signals if s.playbook == PLAYBOOK_KIJUN_PULLBACK],
        direction=direction, min_rr=min_rr, structure_max=structure_max,
    )
    playbook_blocks: Dict[str, Dict[str, Any]] = {}
    for pb in RESEARCH_PLAYBOOKS:
        b = _bucketize_signals(
            [s for s in signals if s.playbook == pb],
            direction=direction, min_rr=min_rr, structure_max=structure_max,
        )
        playbook_blocks[pb] = {
            "label": PLAYBOOK_LABELS.get(pb, pb),
            "research": True,
            "totalAPlus": len(b["aplus"]),
            "actionableCount": len(b["actionable"]),
            "structureCount": len(b["structure"]),
            "structureTotal": b["structure_total"],
            "rejectedCount": b["rejected_count"],
            "subRRRejected": b["sub_rr_count"],
            "actionable": _preview_dicts(b["actionable"]),
            "structure": _preview_dicts(b["structure"]),
        }

    elapsed_ms = int((time.time() - start_time) * 1000)
    result = {
        "asOfDate": as_of_str,
        "scannedCount": len(universe),
        "totalAPlus": len(core["aplus"]),
        "actionableCount": len(core["actionable"]),
        "structureCount": len(core["structure"]),
        "structureTotal": core["structure_total"],
        "rejectedCount": core["rejected_count"],
        "actionable": _preview_dicts(core["actionable"]),
        "structure": _preview_dicts(core["structure"]),
        "playbooks": playbook_blocks,
        "indexState": {"spx": index_spx, "ndx": index_ndx},
        "sectorStates": sector_states,
        "preview": {
            "isPreview": True,
            "asOf": now_iso,
            "marketOpen": market_open,
            "minutesToClose": mins_to_close,
            "quotedCount": len(snaps),
            "syntheticBars": synthetic_count,
            "note": (
                "Signals assume today's candle closes at the current live price. "
                "Volume is the running session total — confirm the close."
            ),
        },
        "meta": {
            "scanDurationMs": elapsed_ms,
            "direction": direction,
            "minDollarAdv": min_dollar_adv,
            "structureMax": structure_max,
            "minRR": min_rr,
            "subRRRejected": core["sub_rr_count"],
            "rsLookback": rs_lookback,
            "betaLookback": beta_lookback,
            "errors": errors[:10] if errors else [],
        },
    }

    with _preview_cache_lock:
        _preview_cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# Desk Trade Tracker (Redis-aware lifecycle + desk-managed states)
# ---------------------------------------------------------------------------

def _persist_signals(signal_dicts: List[Dict[str, Any]]) -> None:
    """Persist freshly scanned signals for later outcome tracking.

    Never downgrades a signal that already has a terminal/triggered status or
    a desk-managed state — the trader's view of an in-flight position wins.
    """
    if not signal_dicts:
        return
    from backend.redis_store import get_store_optional

    # Defensive: accept IchimokuSignal dataclasses too, so a caller that
    # forgets to serialize can't 500 the whole scan.
    signal_dicts = [
        d if isinstance(d, dict) else signal_to_dict(d)
        for d in signal_dicts
    ]

    store = get_store_optional()
    protected = _TERMINAL_STATUSES | {"triggered"} | DESK_STATUSES
    with _signal_store_lock:
        index_keys = set()
        if store:
            index_keys = set(store.get_json(_REDIS_INDEX) or [])
        for d in signal_dicts:
            ticker = d.get("ticker", "")
            sig_date = d.get("signalDate", "")
            if not ticker or not sig_date:
                continue
            key = _signal_key(ticker, sig_date)

            # Redis is the source of truth across gunicorn workers; the
            # per-worker in-memory store is only a no-Redis fallback. Reading
            # in-memory first would let a stale worker copy clobber a desk
            # state another worker just wrote.
            if store:
                prior = store.get_json(_REDIS_PREFIX + key)
                if prior is None:
                    prior = _signal_store.get(key)
            else:
                prior = _signal_store.get(key)
            if prior and prior.get("status") in protected:
                # Refresh the scored snapshot but keep desk/lifecycle state.
                merged = dict(d)
                for fld in ("status", "deskNotes", "outcome", "statusUpdatedAt",
                            "trackedAt", "pinned", "invalidationReason"):
                    if fld in prior:
                        merged[fld] = prior[fld]
                _signal_store[key] = merged
                if store:
                    store.set_json(_REDIS_PREFIX + key, merged, ttl_s=_SIGNAL_TTL_S)
                    index_keys.add(key)
                continue

            record = dict(d)
            record.setdefault("status", "pending")
            record["trackedAt"] = dt.datetime.utcnow().isoformat() + "Z"
            _signal_store[key] = record
            if store:
                store.set_json(_REDIS_PREFIX + key, record, ttl_s=_SIGNAL_TTL_S)
                index_keys.add(key)
        if store:
            store.set_json(_REDIS_INDEX, sorted(index_keys), ttl_s=_SIGNAL_TTL_S)


def _all_records() -> List[Dict[str, Any]]:
    """Return every tracked signal, preferring Redis when present."""
    from backend.redis_store import get_store_optional

    store = get_store_optional()
    records: Dict[str, Dict[str, Any]] = {}
    if store:
        for key in (store.get_json(_REDIS_INDEX) or []):
            rec = store.get_json(_REDIS_PREFIX + key)
            if rec:
                records[key] = rec
    with _signal_store_lock:
        for key, rec in _signal_store.items():
            records.setdefault(key, rec)
    return list(records.values())


def _write_record(key: str, record: Dict[str, Any]) -> None:
    with _signal_store_lock:
        _signal_store[key] = record
    from backend.redis_store import get_store_optional

    store = get_store_optional()
    if store:
        store.set_json(_REDIS_PREFIX + key, record, ttl_s=_SIGNAL_TTL_S)
        index = set(store.get_json(_REDIS_INDEX) or [])
        index.add(key)
        store.set_json(_REDIS_INDEX, sorted(index), ttl_s=_SIGNAL_TTL_S)


def _find_record(ticker: str, signal_date: Optional[str]) -> Optional[Dict[str, Any]]:
    """Locate a tracked record by ticker (+ optional signal date)."""
    ticker = (ticker or "").upper()
    candidates = [r for r in _all_records() if (r.get("ticker") or "").upper() == ticker]
    if not candidates:
        return None
    if signal_date:
        for r in candidates:
            if str(r.get("signalDate", ""))[:10] == str(signal_date)[:10]:
                return r
    # Most recent signal date wins.
    candidates.sort(key=lambda r: str(r.get("signalDate", "")), reverse=True)
    return candidates[0]


def get_all_signals() -> Dict[str, Any]:
    """Group tracked Ichimoku signals by lifecycle + desk-managed status."""
    records = _all_records()
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "pending": [], "triggered": [], "target_hit": [], "stopped": [],
        "invalidated": [], "expired": [],
        "watching": [], "entered": [], "working": [], "broken": [], "exited": [],
    }
    for rec in records:
        status = rec.get("status", "pending")
        buckets.setdefault(status, []).append(rec)

    resolved = buckets["target_hit"] + buckets["stopped"]
    wins = len(buckets["target_hit"])
    win_rate = round(100.0 * wins / len(resolved), 1) if resolved else None
    # "Desk book" = anything the trader is actively managing.
    desk_book = sum(len(buckets[s]) for s in DESK_STATUSES)

    return {
        "totalSignals": len(records),
        "counts": {k: len(v) for k, v in buckets.items()},
        "winRate": win_rate,
        "resolvedCount": len(resolved),
        "deskBookCount": desk_book,
        **buckets,
    }


def set_desk_status(
    ticker: str,
    *,
    desk_status: str,
    signal_date: Optional[str] = None,
    note: Optional[str] = None,
    pinned: Optional[bool] = None,
    signal: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Desk override: mark a name watching/entered/working/broken/exited.

    Returns {ok, record|error}. Desk states survive scan refreshes and are
    never clobbered by the auto-evaluator.

    ``signal`` (optional): full signal dict from the card. Research playbooks
    (TK cross, Kumo breakout) are never auto-persisted by the scan, so their
    first manual Watch seeds the tracker record from the card payload.
    """
    desk_status = (desk_status or "").strip().lower()
    if desk_status not in DESK_STATUSES:
        return {"ok": False, "error": f"Invalid desk status '{desk_status}'. Allowed: {sorted(DESK_STATUSES)}"}

    rec = _find_record(ticker, signal_date)
    if rec is None and isinstance(signal, dict):
        sig_ticker = str(signal.get("ticker") or "").upper()
        if sig_ticker == (ticker or "").upper() and signal.get("signalDate"):
            _persist_signals([signal])
            rec = _find_record(ticker, signal_date or signal.get("signalDate"))
    if rec is None:
        return {"ok": False, "error": f"No tracked signal for {ticker}."}

    rec = dict(rec)
    rec["status"] = desk_status
    rec["statusUpdatedAt"] = dt.datetime.utcnow().isoformat() + "Z"
    if pinned is not None:
        rec["pinned"] = bool(pinned)
    if note:
        notes = list(rec.get("deskNotes", []))
        notes.append({"ts": rec["statusUpdatedAt"], "status": desk_status, "note": note})
        rec["deskNotes"] = notes

    _write_record(_signal_key(rec.get("ticker", ""), rec.get("signalDate", "")), rec)
    return {"ok": True, "record": rec}


def remove_signal(ticker: str, signal_date: Optional[str] = None) -> Dict[str, Any]:
    """Remove a tracked signal from the desk book entirely (e.g. a mis-click)."""
    rec = _find_record(ticker, signal_date)
    if rec is None:
        return {"ok": False, "error": f"No tracked signal for {ticker}."}
    key = _signal_key(rec.get("ticker", ""), rec.get("signalDate", ""))

    with _signal_store_lock:
        _signal_store.pop(key, None)
    from backend.redis_store import get_store_optional

    store = get_store_optional()
    if store:
        store.delete_key(_REDIS_PREFIX + key)
        index = set(store.get_json(_REDIS_INDEX) or [])
        index.discard(key)
        store.set_json(_REDIS_INDEX, sorted(index), ttl_s=_SIGNAL_TTL_S)
    return {"ok": True, "removed": key}


def refresh_signal_statuses(
    as_of_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Re-evaluate every auto-tracked signal against forward price action.

    Desk-managed states are left untouched (the trader owns those); only
    pending/triggered signals get auto-promoted to target_hit/stopped/expired.
    """
    from backend.engine3_red_dog import evaluate_outcome

    today = dt.date.today()
    if as_of_date:
        try:
            today = dt.date.fromisoformat(str(as_of_date)[:10])
        except Exception:
            today = dt.date.today()

    updated = 0
    changed = 0
    for rec in _all_records():
        status = rec.get("status")
        if status in _TERMINAL_STATUSES or status in DESK_STATUSES:
            continue
        ticker = rec.get("ticker", "")
        sig_date = rec.get("signalDate", "")
        levels = rec.get("levels", {}) or {}
        direction = rec.get("direction", "")
        if not ticker or not sig_date:
            continue

        try:
            bars = fetch_bars_for_ticker(ticker=ticker, as_of_date=today, use_cache=False)
        except Exception:
            continue
        forward = [b for b in bars if str(b.trade_date)[:10] > str(sig_date)[:10]]

        # Fold in the live price as a synthetic current-day bar so an intraday
        # trigger/stop is caught immediately instead of waiting for the daily
        # bar to close. Without this, a name can read "pending" all session
        # even though price already blew through the entry.
        try:
            ctx = fetch_live_price_context_optional(ticker=ticker)
            live_px = ctx.get("price") if isinstance(ctx, dict) else None
        except Exception:
            live_px = None
        if live_px is not None and float(live_px) > 0:
            today_str = today.isoformat()
            has_today = any(str(b.trade_date)[:10] == today_str for b in forward)
            if not has_today and today_str > str(sig_date)[:10]:
                px = float(live_px)
                forward.append(DailyBar(
                    trade_date=today_str, open=px, high=px, low=px,
                    close=px, volume=None, vwap=None,
                ))

        if not forward:
            continue

        outcome = evaluate_outcome(
            direction=direction,
            entry_trigger=float(levels.get("entryTrigger") or 0.0),
            stop_loss=float(levels.get("stopLoss") or 0.0),
            target_1=float(levels.get("target1") or 0.0),
            forward_bars=forward,
        )
        new_status = outcome["status"]
        updated += 1
        if new_status != status:
            rec = dict(rec)
            rec["status"] = new_status
            rec["outcome"] = outcome
            rec["statusUpdatedAt"] = dt.datetime.utcnow().isoformat() + "Z"
            _write_record(_signal_key(ticker, sig_date), rec)
            changed += 1

    return {
        "updated": updated,
        "changed": changed,
        "asOfDate": today.isoformat(),
    }
