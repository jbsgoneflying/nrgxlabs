"""Engine 7 – Thematic Relative Value (Pairs) Engine: scan orchestration.

Follows the Engine 3/4 screener pattern:
- TTLCache for bars (6 h) and scan results (30 min)
- ThreadPoolExecutor for parallel bar fetching
- Deterministic theme classification (INV-1)
- NOT_ELIGIBLE bucketing (INV-2)
- Exposure overlap filter (INV-3)
- ORATS graceful degradation (INV-5)
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Tuple

from cachetools import TTLCache

from backend.engine7_pairs import (
    PairDefinition,
    PairSignal,
    analyze_pair,
    build_signal,
    compute_ratio_series,
    load_pair_library,
)
from backend.engine7_theme import (
    ThemeResult,
    annotate_themes_llm,
    classify_themes_deterministic,
    fetch_headlines,
    score_theme_alignment,
)

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Caches (module-level singletons)
# Bump _CACHE_VERSION when keyword lists, scoring weights, or bucketing logic
# change to prevent stale results from replaying.
# ---------------------------------------------------------------------------

_CACHE_VERSION = "v4"

_bars_cache: TTLCache = TTLCache(maxsize=200, ttl=6 * 3600)
_bars_cache_lock = threading.Lock()

_scan_cache: TTLCache = TTLCache(maxsize=20, ttl=30 * 60)
_scan_cache_lock = threading.Lock()

_theme_cache: TTLCache = TTLCache(maxsize=10, ttl=30 * 60)
_theme_cache_lock = threading.Lock()


def clear_engine7_caches() -> None:
    """Force-clear all in-memory caches (scan + theme).  Bars kept."""
    with _scan_cache_lock:
        _scan_cache.clear()
    with _theme_cache_lock:
        _theme_cache.clear()
    _LOG.info("Engine7 in-memory caches cleared (scan + theme)")


# ---------------------------------------------------------------------------
# Bar fetching
# ---------------------------------------------------------------------------


def _cache_key_bars(ticker: str, date_str: str) -> str:
    return f"e7bars:{ticker}:{date_str}"


def fetch_bars_for_tickers(
    tickers: List[str],
    as_of_date: dt.date,
    lookback_days: int = 90,
    max_workers: int = 8,
) -> Dict[str, list]:
    """Fetch daily bars for a list of tickers.  Cached per ticker+date."""
    from backend.technicals import fetch_daily_bars_range

    result: Dict[str, list] = {}
    missing: List[str] = []

    date_str = as_of_date.isoformat()
    for t in tickers:
        ck = _cache_key_bars(t, date_str)
        with _bars_cache_lock:
            cached = _bars_cache.get(ck)
        if cached is not None:
            result[t] = cached
        else:
            missing.append(t)

    if not missing:
        return result

    start = as_of_date - dt.timedelta(days=lookback_days + 30)

    def _fetch_one(ticker: str) -> Tuple[str, list]:
        try:
            bars = fetch_daily_bars_range(ticker=ticker, start=start, end=as_of_date)
            return ticker, bars
        except Exception as exc:
            _LOG.warning("Engine7 bar fetch failed for %s: %s", ticker, exc)
            return ticker, []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_fetch_one, t): t for t in missing}
        for fut in as_completed(futs):
            try:
                ticker, bars = fut.result()
                ck = _cache_key_bars(ticker, date_str)
                with _bars_cache_lock:
                    _bars_cache[ck] = bars
                result[ticker] = bars
            except Exception as exc:
                t = futs[fut]
                _LOG.warning("Engine7 bar fetch future failed for %s: %s", t, exc)
                result[t] = []

    return result


# ---------------------------------------------------------------------------
# ORATS IV overlay (INV-5: graceful degradation)
# ---------------------------------------------------------------------------


def _fetch_orats_overlay(ticker: str) -> Optional[Dict[str, Any]]:
    """Best-effort IV context from ORATS.  Returns None on any failure."""
    try:
        import os
        from backend.orats_client import OratsClient
        token = os.getenv("ORATS_TOKEN", "")
        if not token:
            return None
        client = OratsClient(token=token)
        resp = client.live_summaries(ticker=ticker.upper())
        rows = resp.rows or []
        row = next((r for r in rows if isinstance(r, dict)), None)
        if not row:
            return None
        iv_rank = row.get("ivRank") or row.get("iv_rank")
        if iv_rank is None:
            return None
        return {"iv_rank": float(iv_rank), "ticker": ticker}
    except Exception:
        return None


def _fetch_orats_for_pair(
    long_ticker: str,
    short_ticker: str,
) -> Optional[Dict[str, Any]]:
    """Fetch ORATS for both legs.  Returns merged overlay or None."""
    long_ov = _fetch_orats_overlay(long_ticker)
    short_ov = _fetch_orats_overlay(short_ticker)
    if long_ov is None or short_ov is None:
        return None
    avg_rank = (long_ov["iv_rank"] + short_ov["iv_rank"]) / 2.0
    return {
        "iv_rank": avg_rank,
        "long_iv_rank": long_ov["iv_rank"],
        "short_iv_rank": short_ov["iv_rank"],
    }


# ---------------------------------------------------------------------------
# Exposure overlap filter (INV-3)
# ---------------------------------------------------------------------------


def _compute_ratio_return_correlation(
    signals: List[PairSignal],
    all_bars: Dict[str, list],
    window: int = 20,
) -> Dict[Tuple[str, str], float]:
    """Compute pairwise Pearson correlation of ratio daily returns.

    Returns dict of ((pair_a, pair_b), correlation) for all qualified pairs.
    """
    # Build ratio return series for each signal
    ratio_returns: Dict[str, List[float]] = {}
    for sig in signals:
        bars_l = all_bars.get(sig.long_asset, [])
        bars_s = all_bars.get(sig.short_asset, [])
        dates, ratios = compute_ratio_series(bars_l, bars_s)
        if len(ratios) < window + 1:
            continue
        tail = ratios[-(window + 1):]
        rets = [(tail[i] / tail[i - 1] - 1.0) if tail[i - 1] != 0 else 0.0
                for i in range(1, len(tail))]
        ratio_returns[sig.pair_id] = rets

    corrs: Dict[Tuple[str, str], float] = {}
    pair_ids = list(ratio_returns.keys())
    for i in range(len(pair_ids)):
        for j in range(i + 1, len(pair_ids)):
            a_id, b_id = pair_ids[i], pair_ids[j]
            ra, rb = ratio_returns[a_id], ratio_returns[b_id]
            n = min(len(ra), len(rb))
            if n < 5:
                continue
            ra_n, rb_n = ra[-n:], rb[-n:]
            mu_a = sum(ra_n) / n
            mu_b = sum(rb_n) / n
            cov = sum((ra_n[k] - mu_a) * (rb_n[k] - mu_b) for k in range(n)) / n
            std_a = math.sqrt(sum((v - mu_a) ** 2 for v in ra_n) / n)
            std_b = math.sqrt(sum((v - mu_b) ** 2 for v in rb_n) / n)
            if std_a > 0 and std_b > 0:
                corrs[(a_id, b_id)] = cov / (std_a * std_b)

    return corrs


def check_exposure_overlap(
    signals: List[PairSignal],
    all_bars: Dict[str, list],
    *,
    max_concurrent: int = 5,
    corr_threshold: float = 0.70,
    corr_window: int = 20,
) -> List[PairSignal]:
    """Apply the two-layer exposure overlap filter (INV-3).

    Layer 1: ticker overlap heuristic.
    Layer 2: ratio-return rolling correlation.

    Returns signals with overlap_flags populated and excess pairs demoted.
    """
    if not signals:
        return []

    # Sort by confidence descending for priority
    ranked = sorted(signals, key=lambda s: s.confidence_score, reverse=True)

    # Layer 1: Ticker overlap detection
    ticker_overlap: Dict[str, List[str]] = defaultdict(list)
    for sig in ranked:
        ticker_overlap[sig.long_asset].append(sig.pair_id)
        ticker_overlap[sig.short_asset].append(sig.pair_id)

    overlap_map: Dict[str, List[str]] = defaultdict(list)
    for ticker, pair_ids in ticker_overlap.items():
        if len(pair_ids) > 1:
            for pid in pair_ids:
                others = [p for p in pair_ids if p != pid]
                for o in others:
                    flag = f"ticker_overlap:{ticker}:with:{o}"
                    if flag not in overlap_map[pid]:
                        overlap_map[pid].append(flag)

    # Layer 2: Ratio-return correlation
    corrs = _compute_ratio_return_correlation(ranked, all_bars, window=corr_window)
    for (a_id, b_id), corr_val in corrs.items():
        if abs(corr_val) >= corr_threshold:
            flag_a = f"ratio_corr:{b_id}:{round(corr_val, 3)}"
            flag_b = f"ratio_corr:{a_id}:{round(corr_val, 3)}"
            if flag_a not in overlap_map[a_id]:
                overlap_map[a_id].append(flag_a)
            if flag_b not in overlap_map[b_id]:
                overlap_map[b_id].append(flag_b)

    # Attach flags to signals
    flagged: List[PairSignal] = []
    for sig in ranked:
        flags = tuple(overlap_map.get(sig.pair_id, []))
        if flags != sig.overlap_flags:
            sig = PairSignal(
                pair_id=sig.pair_id,
                long_asset=sig.long_asset,
                short_asset=sig.short_asset,
                tier=sig.tier,
                label=sig.label,
                signal_date=sig.signal_date,
                mode=sig.mode,
                confidence_score=sig.confidence_score,
                grade=sig.grade,
                eligibility=sig.eligibility,
                ineligibility_reason=sig.ineligibility_reason,
                tradable=sig.tradable,
                risk_units=sig.risk_units,
                expected_hold_days=sig.expected_hold_days,
                theme_tags=sig.theme_tags,
                llm_annotation=sig.llm_annotation,
                z_score=sig.z_score,
                momentum_5d_roc=sig.momentum_5d_roc,
                momentum_10d_roc=sig.momentum_10d_roc,
                ratio_current=sig.ratio_current,
                ratio_mean=sig.ratio_mean,
                ratio_std=sig.ratio_std,
                orats_available=sig.orats_available,
                orats_overlay=sig.orats_overlay,
                overlap_flags=flags,
                score_z=sig.score_z,
                score_momentum=sig.score_momentum,
                score_trend=sig.score_trend,
                score_theme=sig.score_theme,
                score_orats=sig.score_orats,
                price_only_score=sig.price_only_score,
            )
        flagged.append(sig)

    # Prune to max_concurrent: keep top-scoring, drop overlapping lower-scored first
    if len([s for s in flagged if s.tradable]) > max_concurrent:
        tradable = [s for s in flagged if s.tradable]
        tradable.sort(key=lambda s: s.confidence_score, reverse=True)

        kept: List[str] = []
        demoted_ids: set = set()
        for sig in tradable:
            if len(kept) >= max_concurrent:
                demoted_ids.add(sig.pair_id)
                continue
            # If this pair has overlap with an already-kept pair, it's the
            # lower-priority one (we iterate by score desc), so demote it
            has_conflict = False
            for flag in sig.overlap_flags:
                for kept_id in kept:
                    if kept_id in flag:
                        has_conflict = True
                        break
                if has_conflict:
                    break
            if has_conflict and len(kept) + 1 > max_concurrent:
                demoted_ids.add(sig.pair_id)
            else:
                kept.append(sig.pair_id)

        # Demote excess pairs
        final: List[PairSignal] = []
        for sig in flagged:
            if sig.pair_id in demoted_ids and sig.tradable:
                sig = PairSignal(
                    pair_id=sig.pair_id,
                    long_asset=sig.long_asset,
                    short_asset=sig.short_asset,
                    tier=sig.tier,
                    label=sig.label,
                    signal_date=sig.signal_date,
                    mode=sig.mode,
                    confidence_score=sig.confidence_score,
                    grade=sig.grade,
                    eligibility=sig.eligibility,
                    ineligibility_reason=sig.ineligibility_reason,
                    tradable=False,
                    risk_units=sig.risk_units,
                    expected_hold_days=sig.expected_hold_days,
                    theme_tags=sig.theme_tags,
                    llm_annotation=sig.llm_annotation,
                    z_score=sig.z_score,
                    momentum_5d_roc=sig.momentum_5d_roc,
                    momentum_10d_roc=sig.momentum_10d_roc,
                    ratio_current=sig.ratio_current,
                    ratio_mean=sig.ratio_mean,
                    ratio_std=sig.ratio_std,
                    orats_available=sig.orats_available,
                    orats_overlay=sig.orats_overlay,
                    overlap_flags=sig.overlap_flags + ("demoted:max_concurrent_exceeded",),
                    score_z=sig.score_z,
                    score_momentum=sig.score_momentum,
                    score_trend=sig.score_trend,
                    score_theme=sig.score_theme,
                    score_orats=sig.score_orats,
                    price_only_score=sig.price_only_score,
                )
            final.append(sig)
        return final

    return flagged


# ---------------------------------------------------------------------------
# Full scan orchestration
# ---------------------------------------------------------------------------


def compute_engine7_scan(
    as_of_date: Optional[str] = None,
    *,
    enable_orats: bool = False,
    enable_llm_annotation: bool = False,
    theme_required: bool = True,
    z_score_window: int = 40,
    z_entry_threshold: float = 1.5,
    z_momentum_threshold: float = 1.0,
    min_score: int = 50,
    aplus_threshold: int = 75,
    max_concurrent: int = 5,
    max_workers: int = 8,
    overlap_corr_threshold: float = 0.70,
    overlap_corr_window: int = 20,
    redis_store: Any = None,
) -> Dict[str, Any]:
    """Orchestrate a full Engine 7 scan across the 20-pair universe.

    Returns dict with keys: aPlus, standard, watchlist, ineligible, meta,
    activeThemes.
    """
    today = dt.date.today()
    if as_of_date:
        try:
            today = dt.date.fromisoformat(str(as_of_date)[:10])
        except Exception:
            today = dt.date.today()

    date_str = today.isoformat()

    # Check scan cache (versioned to bust stale results after code changes)
    cache_key = (_CACHE_VERSION, date_str, min_score, z_score_window)
    with _scan_cache_lock:
        cached = _scan_cache.get(cache_key)
    if cached is not None:
        _LOG.debug("Engine7 scan cache HIT for %s", cache_key)
        return cached
    _LOG.info("Engine7 scan cache MISS for %s — running fresh scan", cache_key)

    # 1. Load pair library
    library = load_pair_library()
    if not library:
        return {"aPlus": [], "standard": [], "watchlist": [], "ineligible": [],
                "meta": {"error": "Pair library empty or not found"}, "activeThemes": []}

    # 2. Collect all unique tickers and fetch bars
    all_tickers = list({p.long_ticker for p in library} | {p.short_ticker for p in library})
    max_lookback = max(p.default_lookback_days for p in library) + 60
    all_bars = fetch_bars_for_tickers(all_tickers, today, lookback_days=max_lookback, max_workers=max_workers)

    # 3. Deterministic theme classification (INV-1)
    theme_cache_key = f"theme:{_CACHE_VERSION}:{date_str}"
    with _theme_cache_lock:
        theme_result = _theme_cache.get(theme_cache_key)

    if theme_result is None:
        headlines = fetch_headlines(date_str, lookback_days=7)
        theme_result = classify_themes_deterministic(headlines)
        theme_result.date = date_str
        with _theme_cache_lock:
            _theme_cache[theme_cache_key] = theme_result

    # 4. Optional LLM annotation (INV-1: never affects scoring)
    llm_annotation = None
    if enable_llm_annotation:
        headlines = fetch_headlines(date_str, lookback_days=7)
        llm_annotation = annotate_themes_llm(
            headlines, date_str, store=redis_store,
        )
        theme_result.llm_annotation = llm_annotation

    # 5. Analyze all pairs
    signals: List[PairSignal] = []
    for pair_def in library:
        bars_long = all_bars.get(pair_def.long_ticker, [])
        bars_short = all_bars.get(pair_def.short_ticker, [])

        if not bars_long or not bars_short:
            _LOG.debug("Engine7: skipping %s, missing bar data", pair_def.pair_id)
            continue

        analysis = analyze_pair(
            pair_def, bars_long, bars_short,
            z_score_window=z_score_window,
            z_entry_threshold=z_entry_threshold,
            z_momentum_threshold=z_momentum_threshold,
        )

        # Theme scoring
        theme_score, theme_tags = score_theme_alignment(pair_def.pair_id, theme_result)

        # ORATS overlay (INV-5)
        orats_data = None
        if enable_orats:
            orats_data = _fetch_orats_for_pair(pair_def.long_ticker, pair_def.short_ticker)
            if orats_data is not None:
                analysis.orats_available = True
                analysis.orats_overlay = orats_data
            else:
                _LOG.debug("Engine7: ORATS unavailable for %s, using price-only scoring", pair_def.pair_id)

        sig = build_signal(
            analysis, date_str, theme_score, theme_tags,
            orats_data=orats_data,
            llm_annotation=llm_annotation,
            theme_required=theme_required,
            min_score=min_score,
            aplus_threshold=aplus_threshold,
        )
        signals.append(sig)

    # 6. Exposure overlap filter (INV-3)
    signals = check_exposure_overlap(
        signals, all_bars,
        max_concurrent=max_concurrent,
        corr_threshold=overlap_corr_threshold,
        corr_window=overlap_corr_window,
    )

    # 7. Categorize into output buckets (INV-2)
    a_plus: List[dict] = []
    standard: List[dict] = []
    watchlist: List[dict] = []
    ineligible: List[dict] = []

    for sig in signals:
        d = sig.to_dict()
        if sig.eligibility == "NOT_ELIGIBLE":
            ineligible.append(d)
        elif sig.tradable and sig.confidence_score >= aplus_threshold:
            a_plus.append(d)
        elif sig.tradable:
            standard.append(d)
        else:
            watchlist.append(d)

    # Sort each bucket by confidence descending
    for bucket in (a_plus, standard, watchlist, ineligible):
        bucket.sort(key=lambda x: x.get("confidence_score", 0), reverse=True)

    # Build theme diagnostics (all candidates, not just active)
    theme_diagnostics = []
    for t in theme_result.themes:
        theme_diagnostics.append({
            "theme": t.theme,
            "label": t.label,
            "active": t.active,
            "keyword_hits": t.keyword_hits,
            "intensity": t.intensity,
            "sample_keywords": t.sample_keywords,
        })

    result = {
        "aPlus": a_plus,
        "standard": standard,
        "watchlist": watchlist,
        "ineligible": ineligible,
        "meta": {
            "scanDate": date_str,
            "cacheVersion": _CACHE_VERSION,
            "pairsAnalyzed": len(signals),
            "tradableCount": len(a_plus) + len(standard),
            "aPlusCount": len(a_plus),
            "standardCount": len(standard),
            "watchlistCount": len(watchlist),
            "ineligibleCount": len(ineligible),
            "headlineCount": theme_result.headline_count,
            "headlineFetchLimit": 500,
            "headlineTopics": ["market", "earnings"],
            "headlineWindowStart": (today - dt.timedelta(days=7)).isoformat(),
            "headlineWindowEnd": date_str,
            "headlineSource": "EODHD multi-topic (market + earnings) + Benzinga fallback",
            "recencyDecay": "60% recent (1.0x) / 40% older (0.5x)",
            "activeThemeCount": len(theme_result.active_themes),
            "activeThemeNames": theme_result.active_themes,
            "themeRequired": theme_required,
            "oratsEnabled": enable_orats,
            "llmAnnotationEnabled": enable_llm_annotation,
            "zScoreWindow": z_score_window,
            "maxConcurrentPairs": max_concurrent,
            "overlapCorrThreshold": overlap_corr_threshold,
        },
        "activeThemes": [t.to_dict() for t in theme_result.themes if t.active],
        "themeDiagnostics": theme_diagnostics,
    }

    if llm_annotation:
        result["llmAnnotation"] = llm_annotation

    # Cache result
    with _scan_cache_lock:
        _scan_cache[cache_key] = result

    return result


# ---------------------------------------------------------------------------
# Single pair analysis (for the /{pair_id} endpoint)
# ---------------------------------------------------------------------------


def analyze_single_pair_detail(
    pair_id: str,
    as_of_date: Optional[str] = None,
    *,
    enable_orats: bool = False,
    enable_llm_annotation: bool = False,
    theme_required: bool = True,
    z_score_window: int = 40,
    z_entry_threshold: float = 1.5,
    z_momentum_threshold: float = 1.0,
    min_score: int = 50,
    aplus_threshold: int = 75,
    redis_store: Any = None,
) -> Optional[Dict[str, Any]]:
    """Full analysis for a single pair.  Returns detailed dict or None."""
    library = load_pair_library()
    pair_def = next((p for p in library if p.pair_id == pair_id), None)
    if pair_def is None:
        return None

    today = dt.date.today()
    if as_of_date:
        try:
            today = dt.date.fromisoformat(str(as_of_date)[:10])
        except Exception:
            today = dt.date.today()

    date_str = today.isoformat()
    lookback = pair_def.default_lookback_days + 60
    all_bars = fetch_bars_for_tickers(
        [pair_def.long_ticker, pair_def.short_ticker], today, lookback_days=lookback,
    )
    bars_long = all_bars.get(pair_def.long_ticker, [])
    bars_short = all_bars.get(pair_def.short_ticker, [])

    if not bars_long or not bars_short:
        return {"error": f"Missing bar data for {pair_def.pair_id}"}

    analysis = analyze_pair(
        pair_def, bars_long, bars_short,
        z_score_window=z_score_window,
        z_entry_threshold=z_entry_threshold,
        z_momentum_threshold=z_momentum_threshold,
    )

    # Theme
    headlines = fetch_headlines(date_str, lookback_days=7)
    theme_result = classify_themes_deterministic(headlines)
    theme_result.date = date_str
    theme_score, theme_tags = score_theme_alignment(pair_def.pair_id, theme_result)

    # ORATS
    orats_data = None
    if enable_orats:
        orats_data = _fetch_orats_for_pair(pair_def.long_ticker, pair_def.short_ticker)
        if orats_data:
            analysis.orats_available = True
            analysis.orats_overlay = orats_data

    # LLM annotation
    llm_ann = None
    if enable_llm_annotation:
        llm_ann = annotate_themes_llm(headlines, date_str, store=redis_store)

    sig = build_signal(
        analysis, date_str, theme_score, theme_tags,
        orats_data=orats_data,
        llm_annotation=llm_ann,
        theme_required=theme_required,
        min_score=min_score,
        aplus_threshold=aplus_threshold,
    )

    # Build detailed response with ratio chart data
    return {
        "signal": sig.to_dict(),
        "ratioChart": {
            "dates": analysis.ratio_dates[-120:],
            "values": [round(v, 6) for v in analysis.ratio_series[-120:]],
        },
        "statistics": {
            "ratioMean": round(analysis.ratio_mean, 6),
            "ratioStd": round(analysis.ratio_std, 6),
            "zScore": round(analysis.z_score, 4),
            "momentum5dRoc": round(analysis.momentum_5d_roc, 6),
            "momentum10dRoc": round(analysis.momentum_10d_roc, 6),
            "ratioVsSma20": round(analysis.ratio_vs_sma20, 6),
            "ratioVsSma50": round(analysis.ratio_vs_sma50, 6),
            "trendStructure": analysis.trend_structure,
        },
        "themeAlignment": {
            "score": theme_score,
            "matchingThemes": theme_tags,
            "activeThemes": [t.to_dict() for t in theme_result.themes if t.active],
        },
        "oratsOverlay": analysis.orats_overlay,
        "llmAnnotation": llm_ann,
        "meta": {
            "scanDate": date_str,
            "pairId": pair_def.pair_id,
            "tier": pair_def.tier,
            "label": pair_def.label,
        },
    }
