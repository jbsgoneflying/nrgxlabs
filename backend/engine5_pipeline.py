"""Engine 5 – Pipeline Runner.

Core pipeline logic extracted so it can be called from:
1. The FastAPI /api/engine5/weekly-ideas endpoint (auto-run on first use)
2. The cron script scripts/refresh_engine5_snapshot.py (nightly)

Storage strategy:
- engine5:history:*             -- TTL 180d, append-only raw data cache
- engine5:snapshot:{id}         -- TTL 14d, immutable per-run output
- engine5:snapshots:index       -- JSON list of IDs, newest first
- engine5:pointer:best/latest   -- snapshot ID strings
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from backend.config import get_flags
from backend.eodhd_client import EodhdClient, EodhdError
from backend.engine5_global_intake import (
    GlobalAssetBar,
    YieldSnapshot,
    all_eod_symbols,
    build_yield_snapshot,
    load_universe,
    normalize_bars,
    normalize_bars_bulk,
)
from backend.engine5_lead_lag import compute_lead_lag_signals
from backend.engine5_regime import compute_regime_from_bars
from backend.engine5_translation import translate_signals_to_us
from backend.engine5_idea_generator import generate_weekly_ideas
from backend.engine5_vol_leadlag import compute_vol_leadlag, VolLeadLagResult
from backend.engine5_snapshot import (
    SnapshotMeta,
    compute_asof_dates,
    compute_completeness,
    compute_grade,
    generate_snapshot_id,
    persist_snapshot,
    GRADE_LABELS,
)
from backend.redis_store import get_store_optional

try:
    from backend.orats_client import OratsClient, OratsError
except ImportError:
    OratsClient = None  # type: ignore
    OratsError = Exception  # type: ignore

LOG = logging.getLogger("engine5_pipeline")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _previous_trading_day(ref: dt.date) -> dt.date:
    """Return the most recent weekday before `ref`."""
    d = ref - dt.timedelta(days=1)
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d -= dt.timedelta(days=1)
    return d


def _load_history_from_redis(
    store: Any,
    symbols: List[str],
    ttl_history: int,
) -> Dict[str, List[dict]]:
    """Load durable bar history from Redis for all symbols."""
    history: Dict[str, List[dict]] = {}
    for sym in symbols:
        key = f"engine5:history:{sym}"
        data = store.get_json(key)
        if isinstance(data, list):
            history[sym] = data
        elif isinstance(data, dict):
            history[sym] = list(data.values()) if data else []
        else:
            history[sym] = []
    return history


def _append_bar_to_history(
    store: Any,
    symbol: str,
    bar: dict,
    ttl_history: int,
    max_days: int = 200,
) -> None:
    """Append a single bar to the durable history in Redis."""
    key = f"engine5:history:{symbol}"
    existing = store.get_json(key)
    if not isinstance(existing, list):
        existing = []

    bar_date = bar.get("date", "")
    existing = [b for b in existing if b.get("date") != bar_date]
    existing.append(bar)

    existing.sort(key=lambda b: str(b.get("date", "")))
    if len(existing) > max_days:
        existing = existing[-max_days:]

    store.set_json(key, existing, ttl_s=ttl_history)


# ---------------------------------------------------------------------------
# Data freshness guard
# ---------------------------------------------------------------------------


def _check_freshness(
    client: EodhdClient,
    sentinel_symbol: str,
    expected_date: dt.date,
    retries: int = 3,
    interval_s: int = 900,
) -> Tuple[bool, List[dict]]:
    """Check that the sentinel ticker has data for the expected trading date.

    Returns (is_fresh, bars).
    """
    for attempt in range(1, retries + 1):
        try:
            resp = client.get_eod(
                sentinel_symbol,
                from_date=(expected_date - dt.timedelta(days=5)).isoformat(),
                to_date=expected_date.isoformat(),
            )
            bars = resp.rows
            if bars:
                latest_date = max(str(b.get("date", ""))[:10] for b in bars)
                if latest_date >= expected_date.isoformat():
                    LOG.info("Freshness OK: %s has data for %s", sentinel_symbol, expected_date)
                    return True, bars
                LOG.warning(
                    "Freshness check %d/%d: latest=%s, expected=%s",
                    attempt, retries, latest_date, expected_date,
                )
            else:
                LOG.warning("Freshness check %d/%d: no bars returned for %s", attempt, retries, sentinel_symbol)
        except EodhdError as e:
            LOG.warning("Freshness check %d/%d error: %s", attempt, retries, e)

        if attempt < retries:
            LOG.info("Retrying in %d seconds...", interval_s)
            time.sleep(interval_s)

    LOG.error("Data freshness guard FAILED after %d retries", retries)
    return False, []


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    force: bool = False,
    source: str = "manual",
) -> Tuple[int, Optional[str]]:
    """Execute the full Engine 5 pipeline.

    Returns ``(exit_code, snapshot_id)``.
    - exit_code 0  = success
    - exit_code >0 = failure  (snapshot_id will be None)
    """
    pipeline_start = time.monotonic()
    flags = get_flags()

    if not flags.ENABLE_ENGINE5_LEAD_LAG:
        LOG.info("Engine 5 is disabled (ENABLE_ENGINE5_LEAD_LAG=0). Exiting.")
        return 0, None

    store = get_store_optional()
    if store is None:
        LOG.error("Missing REDIS_URL; cannot run Engine 5 pipeline.")
        return 2, None
    if not store.ping():
        LOG.error("Redis ping failed; cannot run Engine 5 pipeline.")
        return 3, None

    # Gate check: skip if the latest snapshot was created < 20h ago (unless force)
    if not force:
        latest_sid = store.get_json("engine5:pointer:latest")
        if latest_sid and isinstance(latest_sid, str):
            snap = store.get_json(f"engine5:snapshot:{latest_sid}")
            if snap:
                meta = snap.get("meta", {})
                created = meta.get("createdAt", "")
                if created:
                    try:
                        created_dt = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
                        age_h = (dt.datetime.now(dt.timezone.utc) - created_dt).total_seconds() / 3600
                        if age_h < 20:
                            LOG.info("Skipping: latest snapshot %s is %.1fh old", latest_sid, age_h)
                            return 0, latest_sid
                    except (ValueError, TypeError):
                        pass

    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    expected_date = _previous_trading_day(today)

    LOG.info("Starting Engine 5 pipeline for expected_date=%s", expected_date)

    # Load universe
    try:
        universe = load_universe()
    except Exception as e:
        LOG.error("Failed to load universe: %s", e)
        return 1, None

    sentinel = universe.get("sentinel_ticker", "STOXX50E.INDX")

    # Initialize EODHD client
    try:
        eodhd = EodhdClient.from_env()
    except EodhdError as e:
        LOG.error("EODHD client init failed: %s", e)
        return 1, None

    # Step 2: Fetch global EOD bars
    eod_symbols = all_eod_symbols(universe)
    all_symbols = [e["symbol"] for e in eod_symbols]

    # Check if this is a cold start (no history in Redis)
    history = _load_history_from_redis(store, all_symbols, flags.ENGINE5_CACHE_TTL_HISTORY)
    sentinel_hist = history.get(sentinel, [])
    is_cold_start = len(sentinel_hist) < flags.ENGINE5_CORR_WINDOW

    # ---------- Enhanced diagnostics: history depth ----------
    _hist_with_data = sum(1 for v in history.values() if v)
    _hist_empty = sum(1 for v in history.values() if not v)
    _hist_min = min((len(v) for v in history.values() if v), default=0)
    _hist_max = max((len(v) for v in history.values() if v), default=0)
    LOG.info(
        "History loaded: %d symbols with data, %d empty.  "
        "Sentinel (%s) has %d bars.  Range: %d–%d bars.  Cold start: %s",
        _hist_with_data, _hist_empty, sentinel, len(sentinel_hist),
        _hist_min, _hist_max, is_cold_start,
    )
    # ---------------------------------------------------------

    if is_cold_start:
        # Backfill 90 calendar days (~60 trading days) to fill correlation windows
        backfill_days = 90
        LOG.info("Cold start detected (%d bars for sentinel). Backfilling %d days of history...",
                 len(sentinel_hist), backfill_days)
    else:
        backfill_days = 10
        LOG.info("Fetching latest EOD bars (incremental, %d days)...", backfill_days)

    raw_bars: Dict[str, List[dict]] = {}
    fetch_from = (expected_date - dt.timedelta(days=backfill_days)).isoformat()
    fetch_to = expected_date.isoformat()

    def _fetch_one(entry: dict) -> Tuple[str, List[dict]]:
        sym = entry["symbol"]
        try:
            resp = eodhd.get_eod(sym, from_date=fetch_from, to_date=fetch_to)
            return sym, resp.rows
        except EodhdError as e:
            fallback = entry.get("fallback")
            if fallback:
                try:
                    resp = eodhd.get_eod(fallback, from_date=fetch_from, to_date=fetch_to)
                    LOG.info("Used fallback %s for %s", fallback, sym)
                    return sym, resp.rows
                except EodhdError:
                    pass
            LOG.warning("Failed to fetch %s: %s", sym, e)
            return sym, []

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_fetch_one, entry): entry for entry in eod_symbols}
        for fut in as_completed(futures):
            sym, bars = fut.result()
            if bars:
                raw_bars[sym] = bars

    # ---------- Enhanced diagnostics: per-region bar coverage ----------
    _region_counts: Dict[str, int] = {}
    _region_missing: Dict[str, List[str]] = {}
    for entry in eod_symbols:
        sym = entry["symbol"]
        region = entry.get("region", "unknown")
        _region_counts.setdefault(region, 0)
        _region_missing.setdefault(region, [])
        if sym in raw_bars:
            _region_counts[region] += 1
        else:
            _region_missing[region].append(sym)
    LOG.info(
        "Fetched %d/%d symbols (%d days window).  Per-region: %s",
        len(raw_bars), len(eod_symbols), backfill_days,
        {r: f"{c}/{c + len(_region_missing.get(r, []))}" for r, c in _region_counts.items()},
    )
    for region, missing in _region_missing.items():
        if missing:
            LOG.info("  Missing bars [%s]: %s", region, ", ".join(missing[:10]))
    # -------------------------------------------------------------------

    # Step 3: Data freshness guard
    # For on-demand runs, only retry once with 0 interval to keep it fast
    retry_count = 1 if force else flags.ENGINE5_FRESHNESS_RETRY_COUNT
    retry_interval = 0 if force else flags.ENGINE5_FRESHNESS_RETRY_INTERVAL_S

    is_fresh, _ = _check_freshness(
        eodhd,
        sentinel,
        expected_date,
        retries=retry_count,
        interval_s=retry_interval,
    )
    if not is_fresh:
        # Still proceed with whatever data we have; snapshot will be graded C
        LOG.warning("Data may be stale (sentinel check failed); proceeding with available bars.")

    # Step 4: Fetch US yield curve
    LOG.info("Fetching US yield curve...")
    ust_rows: List[dict] = []
    try:
        ust_resp = eodhd.get_ust_yield_rates()
        ust_rows = ust_resp.rows
    except EodhdError as e:
        LOG.warning("UST yield fetch failed: %s", e)

    de_10y_bars = raw_bars.get("DE10Y.GBOND", [])
    jp_10y_bars = raw_bars.get("JP10Y.GBOND", [])

    real_yield_rows: List[dict] = []
    try:
        real_resp = eodhd.get_ust_real_yield_rates()
        real_yield_rows = real_resp.rows
    except EodhdError as e:
        LOG.warning("UST real yield fetch failed: %s", e)

    yield_snapshot = build_yield_snapshot(ust_rows, de_10y_bars, jp_10y_bars, real_yield_rows)

    # Step 5: Extract FX rates
    fx_rates: Dict[str, float] = {}
    for fx_entry in universe.get("fx", []):
        sym = fx_entry["symbol"]
        bars = raw_bars.get(sym, [])
        if bars:
            latest = sorted(bars, key=lambda b: str(b.get("date", "")))[-1]
            close = latest.get("adjusted_close") or latest.get("close")
            if close:
                try:
                    fx_rates[sym] = float(close)
                except (TypeError, ValueError):
                    pass
    LOG.info("FX rates: %s", fx_rates)

    # Step 6: Normalize bars
    if is_cold_start:
        # BACKFILL MODE: process every day sequentially so returns build up
        LOG.info("Bulk-normalizing %d days of bars (backfill)...", len(raw_bars))
        all_processed, updated_history = normalize_bars_bulk(
            raw_bars, fx_rates, history, universe,
        )
        # Latest bars = only the most recent date
        latest_date = max(b.date for b in all_processed) if all_processed else ""
        normalized = [b for b in all_processed if b.date == latest_date]
        LOG.info("Backfill complete: %d total bars across all days, %d on latest date (%s)",
                 len(all_processed), len(normalized), latest_date)

        # Write full history to Redis in one shot per symbol
        LOG.info("Writing backfill history to Redis (%d symbols)...", len(updated_history))
        for sym, hist_bars in updated_history.items():
            # Prune to 200 most recent
            if len(hist_bars) > 200:
                hist_bars = hist_bars[-200:]
            store.set_json(f"engine5:history:{sym}", hist_bars, ttl_s=flags.ENGINE5_CACHE_TTL_HISTORY)

        history = updated_history
    else:
        # INCREMENTAL MODE: only process latest day against existing history
        LOG.info("Normalizing bars (incremental)...")
        normalized = normalize_bars(raw_bars, fx_rates, history, universe)
        LOG.info("Normalized %d bars", len(normalized))

        # Append today's bars to history
        for bar in normalized:
            _append_bar_to_history(
                store, bar.symbol, bar.to_dict(),
                ttl_history=flags.ENGINE5_CACHE_TTL_HISTORY,
            )

        # Reload history with today's bars
        history = _load_history_from_redis(store, all_symbols, flags.ENGINE5_CACHE_TTL_HISTORY)

    # Step 7: Prepare latest bars (no mutable Redis key — goes into snapshot)
    latest_bars_json = [b.to_dict() for b in normalized]

    # Append yield snapshot to durable history (raw data cache)
    if yield_snapshot:
        yield_hist_key = "engine5:history:yields"
        yield_hist = store.get_json(yield_hist_key)
        if not isinstance(yield_hist, list):
            yield_hist = []
        yield_hist = [y for y in yield_hist if y.get("date") != yield_snapshot.date]
        yield_hist.append(yield_snapshot.to_dict())
        yield_hist.sort(key=lambda y: str(y.get("date", "")))
        if len(yield_hist) > 200:
            yield_hist = yield_hist[-200:]
        store.set_json(yield_hist_key, yield_hist, ttl_s=flags.ENGINE5_CACHE_TTL_HISTORY)

    # Step 8: Compute lead-lag signals
    LOG.info("Computing lead-lag signals...")
    leader_returns: Dict[str, List[Tuple[str, float]]] = {}
    follower_returns: Dict[str, List[Tuple[str, float]]] = {}
    mapping: Dict[str, List[str]] = {}

    for entry in universe.get("equity_indices", []):
        sym = entry["symbol"]
        targets = entry.get("us_targets", [])
        mapping[sym] = targets
        hist = history.get(sym, [])
        returns = []
        for b in sorted(hist, key=lambda b: str(b.get("date", ""))):
            r = b.get("return_1d_local")
            d = b.get("date", "")
            if r is not None and d:
                try:
                    returns.append((d, float(r)))
                except (TypeError, ValueError):
                    pass
        if returns:
            leader_returns[sym] = returns

    spy_hist = history.get("GSPC.INDX", [])
    spy_returns = []
    for b in sorted(spy_hist, key=lambda b: str(b.get("date", ""))):
        r = b.get("return_1d_local")
        d = b.get("date", "")
        if r is not None and d:
            try:
                spy_returns.append((d, float(r)))
            except (TypeError, ValueError):
                pass
    if spy_returns:
        follower_returns["SPY"] = spy_returns

    signals = compute_lead_lag_signals(
        leader_returns=leader_returns,
        follower_returns=follower_returns,
        mapping=mapping,
        corr_window=flags.ENGINE5_CORR_WINDOW,
        max_lag_days=flags.ENGINE5_MAX_LAG_DAYS,
        corr_threshold=flags.ENGINE5_CORR_THRESHOLD,
        z_significant=flags.ENGINE5_Z_SIGNIFICANT,
        lookback_days=flags.ENGINE5_LOOKBACK_DAYS,
        date=expected_date.isoformat(),
    )
    LOG.info("Computed %d lead-lag signals", len(signals))
    # ---------- Enhanced diagnostics: signal breakdown ----------
    if signals:
        _sig_dirs: Dict[str, int] = {}
        _sig_targets: Dict[str, int] = {}
        for s in signals:
            d = s.direction
            _sig_dirs[d] = _sig_dirs.get(d, 0) + 1
            t = s.follower_symbol
            _sig_targets[t] = _sig_targets.get(t, 0) + 1
        LOG.info(
            "  Signal directions: %s  |  Targets: %s",
            _sig_dirs, dict(sorted(_sig_targets.items())),
        )
    else:
        LOG.warning(
            "  No signals produced. Leaders with data: %d, SPY returns: %d, "
            "corr_window=%d, corr_threshold=%.2f",
            len(leader_returns), len(follower_returns.get("SPY", [])),
            flags.ENGINE5_CORR_WINDOW, flags.ENGINE5_CORR_THRESHOLD,
        )
    # -----------------------------------------------------------
    signals_json = [s.to_dict() for s in signals]

    # Step 8b: Fetch ORATS IV data for regime and idea generation
    spy_iv_rank = None
    orats_data: Dict[str, dict] = {}
    if OratsClient is not None:
        try:
            orats = OratsClient.from_env()
            LOG.info("Fetching ORATS data (SPY IV rank + sector ETF surfaces)...")

            # SPY IV rank from cores endpoint
            try:
                spy_cores = orats.cores(ticker="SPY", fields="orIvRk20d,orIvRk60d,orIvRk120d,orIvRk252d")
                if spy_cores.rows:
                    row = spy_cores.rows[0]
                    # Use 252d IV rank as the primary regime input (annual perspective)
                    spy_iv_rank = row.get("orIvRk252d") or row.get("orIvRk120d") or row.get("orIvRk60d")
                    if spy_iv_rank is not None:
                        spy_iv_rank = float(spy_iv_rank)
                        LOG.info("SPY IV rank (252d): %.2f", spy_iv_rank)
            except Exception as e:
                LOG.warning("Failed to fetch SPY IV rank: %s", e)

            # Per-sector ORATS data for trade idea enrichment + invalidation
            sector_symbols = set()
            for entry in universe.get("equity_indices", []):
                for t in entry.get("us_targets", []):
                    sector_symbols.add(t)
            for sym in sector_symbols:
                try:
                    cores_resp = orats.cores(
                        ticker=sym,
                        fields="orIvRk252d,orDte,orSmvVol,orFcstCl1m,orDlta,stkPx,orIvFcst",
                    )
                    if cores_resp.rows:
                        r = cores_resp.rows[0]
                        iv_rk = r.get("orIvRk252d")
                        smv_vol = r.get("orSmvVol")
                        stk_px = r.get("stkPx")
                        or_dte = r.get("orDte")
                        or_dlta = r.get("orDlta")
                        or_iv = r.get("orIvFcst") or smv_vol
                        orats_data[sym] = {
                            "iv_rank": float(iv_rk) if iv_rk is not None else None,
                            "expected_move": float(smv_vol) * 100 if smv_vol is not None else None,
                            "close": float(stk_px) if stk_px is not None else None,
                            "dte": int(or_dte) if or_dte is not None else 5,
                            "delta_short": float(or_dlta) if or_dlta is not None else None,
                            "iv": float(or_iv) if or_iv is not None else None,
                            "k_short": None,  # Not available from cores; invalidation will use EM-based level
                        }
                except Exception as e:
                    LOG.warning("ORATS fetch for %s failed: %s", sym, e)

            LOG.info("ORATS data fetched for %d sector symbols", len(orats_data))
        except OratsError as e:
            LOG.warning("ORATS client init failed (continuing without IV data): %s", e)
    else:
        LOG.info("ORATS client not available; skipping IV rank fetch.")

    # Step 9: Classify regime
    LOG.info("Classifying regime...")
    yield_hist = store.get_json("engine5:history:yields")
    if not isinstance(yield_hist, list):
        yield_hist = []

    regime = compute_regime_from_bars(
        date=expected_date.isoformat(),
        bars_history=history,
        yield_snapshots=yield_hist,
        spy_iv_rank=spy_iv_rank,
        stressed_threshold=flags.ENGINE5_REGIME_STRESSED_THRESHOLD,
        risk_off_threshold=flags.ENGINE5_REGIME_RISK_OFF_THRESHOLD,
        transitional_threshold=flags.ENGINE5_REGIME_TRANSITIONAL_THRESHOLD,
    )
    LOG.info("Regime: %s (score=%.1f)", regime.label, regime.score)

    # Step 10: Translate to US bias
    LOG.info("Translating to US biases...")
    fx_bar_history: Dict[str, List[dict]] = {}
    for fx_entry in universe.get("fx", []):
        sym = fx_entry["symbol"]
        fx_bar_history[sym] = history.get(sym, [])

    sector_biases, index_biases = translate_signals_to_us(
        signals=signals_json,
        regime=regime.to_dict(),
        yield_snapshot=yield_snapshot.to_dict() if yield_snapshot else None,
        fx_bars=fx_bar_history,
    )
    LOG.info("Generated %d sector biases, %d index biases", len(sector_biases), len(index_biases))
    # ---------- Enhanced diagnostics: bias breakdown ----------
    if sector_biases:
        for sb in sector_biases:
            LOG.info(
                "  Sector bias: %s → %s (conf=%d, sources=%d)",
                sb.sector, sb.direction, sb.confidence, len(sb.sources),
            )
    else:
        LOG.warning("  No sector biases generated from %d signals + overlays.", len(signals))
    # ----------------------------------------------------------

    # Step 10b: Compute Vol Lead-Lag
    vol_result: VolLeadLagResult | None = None
    if flags.ENGINE5_VOL_LEADLAG_ENABLED:
        LOG.info("Computing vol lead-lag...")
        vol_result = compute_vol_leadlag(
            bars_history=history,
            universe=universe,
            spy_iv_rank=spy_iv_rank,
            rising_threshold=flags.ENGINE5_GLOBAL_VOL_RISING_THRESHOLD,
            falling_threshold=flags.ENGINE5_GLOBAL_VOL_FALLING_THRESHOLD,
            noise_floor=flags.ENGINE5_GLOBAL_VOL_NOISE_FLOOR,
            iv_low_threshold=flags.ENGINE5_US_IV_LOW_THRESHOLD,
            iv_high_threshold=flags.ENGINE5_US_IV_HIGH_THRESHOLD,
            zscore_window=flags.ENGINE5_VOL_ZSCORE_WINDOW,
        )
        LOG.info("Vol lead-lag: state=%s, score=%.2f, suppressed=%s",
                 vol_result.vol_lag_state, vol_result.global_vol_score, vol_result.suppressed)
    else:
        LOG.info("Vol lead-lag module disabled.")

    # Step 11: Build invalidation inputs and generate weekly ideas
    LOG.info("Generating weekly ideas...")

    # Build yield curve series for driver invalidation (2s10s slope history)
    yield_curve_series: List[float] = []
    for ys in yield_hist:
        slope = ys.get("us_2s10s_slope")
        if slope is not None:
            try:
                yield_curve_series.append(float(slope))
            except (TypeError, ValueError):
                pass

    # Compute 3-day stress changes for driver invalidation
    # We need current and 3-day-ago regime components. We'll use yield_hist
    # entries as proxy timeline, but the real source is regime history stored
    # in engine5:history:regime_components.
    stress_3d_changes: Dict[str, float] = {}
    regime_comp_hist_key = "engine5:history:regime_components"
    regime_comp_hist = store.get_json(regime_comp_hist_key)
    if not isinstance(regime_comp_hist, list):
        regime_comp_hist = []

    # Append current regime components to history
    current_comp_entry = {
        "date": expected_date.isoformat(),
        **regime.components,
    }
    regime_comp_hist = [e for e in regime_comp_hist if e.get("date") != expected_date.isoformat()]
    regime_comp_hist.append(current_comp_entry)
    regime_comp_hist.sort(key=lambda e: str(e.get("date", "")))
    if len(regime_comp_hist) > 200:
        regime_comp_hist = regime_comp_hist[-200:]
    store.set_json(regime_comp_hist_key, regime_comp_hist, ttl_s=flags.ENGINE5_CACHE_TTL_HISTORY)

    # Calculate 3-day changes
    if len(regime_comp_hist) >= 4:
        three_ago = regime_comp_hist[-4]
        for comp_key in ("fx_stress", "yield_stress", "commodity_stress", "iv_stress"):
            cur_val = regime.components.get(comp_key)
            old_val = three_ago.get(comp_key)
            if cur_val is not None and old_val is not None:
                try:
                    stress_3d_changes[comp_key] = float(cur_val) - float(old_val)
                except (TypeError, ValueError):
                    pass
    LOG.info("Stress 3D changes: %s", stress_3d_changes)

    ideas = generate_weekly_ideas(
        date=expected_date.isoformat(),
        signals=signals_json,
        regime=regime,
        sector_biases=sector_biases,
        index_biases=index_biases,
        bars=latest_bars_json,
        orats_data=orats_data,
        yield_curve_series=yield_curve_series if yield_curve_series else None,
        stress_3d_changes=stress_3d_changes if stress_3d_changes else None,
        vol_leadlag=vol_result,
    )
    ideas_output = ideas.to_dict()
    _idea_count = len(ideas_output.get("tradeIdeas", []))
    _bias_count = len(ideas_output.get("sectorBiases", []))
    LOG.info("Generated weekly ideas: %d trade ideas, %d sector biases", _idea_count, _bias_count)
    # ---------- Enhanced diagnostics: idea detail ----------
    for _idea in ideas_output.get("tradeIdeas", []):
        LOG.info(
            "  Idea: %s %s conf=%s structure=%s suppressed=%s invalidation=%s",
            _idea.get("symbol"), _idea.get("directionalLean"),
            _idea.get("confidence"), _idea.get("structure"),
            _idea.get("suppressed", False), _idea.get("invalidationStatus", "—"),
        )
    if _idea_count == 0:
        LOG.warning(
            "  Zero trade ideas.  Input biases=%d (min conf for idea=30). "
            "Regime=%s (modifier=%.2f).",
            len(sector_biases), regime.label,
            regime.to_dict().get("position_size_modifier", 1.0),
        )
    # ------------------------------------------------------

    # ------------------------------------------------------------------
    # Step 12: Build and persist immutable snapshot
    # ------------------------------------------------------------------
    pipeline_duration = time.monotonic() - pipeline_start
    snapshot_id = generate_snapshot_id(now)

    # As-of dates per region
    asof_dates = compute_asof_dates(latest_bars_json, universe)

    # Freshness grade
    grade = compute_grade(asof_dates, is_stale=not is_fresh)
    grade_label = GRADE_LABELS.get(grade, "")

    # Completeness score
    completeness = compute_completeness(ideas_output)

    meta = SnapshotMeta(
        snapshot_id=snapshot_id,
        created_at_utc=now.isoformat(),
        asof_dates=asof_dates,
        grade=grade,
        grade_label=grade_label,
        completeness=completeness,
        regime_label=regime.label,
        trade_ideas_count=len(ideas_output.get("tradeIdeas", [])),
        is_stale=not is_fresh,
        pipeline_duration_s=round(pipeline_duration, 2),
        source=source,
        warning=None if grade in ("A", "B") else "Partial data — some regions may not have fresh closes.",
    )

    ok = persist_snapshot(
        store=store,
        snapshot_id=snapshot_id,
        meta=meta,
        data=ideas_output,
        snapshot_ttl=flags.ENGINE5_SNAPSHOT_TTL_S,
        index_ttl=flags.ENGINE5_SNAPSHOT_INDEX_TTL_S,
        max_index=flags.ENGINE5_SNAPSHOT_MAX_INDEX,
    )

    if not ok:
        LOG.error("Failed to persist snapshot %s", snapshot_id)
        return 1, None

    LOG.info(
        "Engine 5 pipeline complete. snapshot=%s grade=%s completeness=%.2f "
        "date=%s regime=%s signals=%d biases=%d ideas=%d asof=%s (%.1fs)",
        snapshot_id, grade, completeness,
        expected_date, regime.label, len(signals),
        len(sector_biases), len(ideas_output.get("tradeIdeas", [])),
        asof_dates, pipeline_duration,
    )
    return 0, snapshot_id
