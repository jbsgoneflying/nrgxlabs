#!/usr/bin/env python3
"""Raven-Tech Front Layer – Historical Backfill Script.

Fetches 14 days of historical cross-asset bars, news headlines, and calendar
events from EODHD + Benzinga, builds a DailyMarketState for each trading day,
and persists everything to Redis so Monday's first cron run (and the LLM)
have a solid 2-week history to work from.

Safe to re-run: overwrites existing DMS entries for the same dates.

API cost:
  - EODHD:    ~12 calls  (11 cross-asset symbols + S&P 500, date-range)
  - Benzinga: ~16 calls  (14 days of news + 2 calendar bulk calls)
  - Total:    ~30 calls, runtime ~2-3 minutes

Usage:
    python scripts/backfill_front_layer.py [--days N]

    --days N   Number of calendar days to backfill (default 14)
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import time

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# Ensure repo root is on sys.path for cron-friendly execution.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
LOG = logging.getLogger("backfill_front_layer")

# Default lookback in calendar days
DEFAULT_LOOKBACK_DAYS = 14


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _parse_days_arg() -> int:
    """Parse --days N from argv."""
    for i, arg in enumerate(sys.argv):
        if arg == "--days" and i + 1 < len(sys.argv):
            try:
                return max(1, int(sys.argv[i + 1]))
            except ValueError:
                pass
    return DEFAULT_LOOKBACK_DAYS


def _trading_dates_in_range(start: dt.date, end: dt.date) -> list[dt.date]:
    """Return weekday dates in [start, end] inclusive, sorted chronologically."""
    dates = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            dates.append(d)
        d += dt.timedelta(days=1)
    return dates


def _calendar_dates_in_range(start: dt.date, end: dt.date) -> list[dt.date]:
    """Return ALL dates in [start, end] inclusive (for news / themes)."""
    dates = []
    d = start
    while d <= end:
        dates.append(d)
        d += dt.timedelta(days=1)
    return dates


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def _fetch_cross_asset_bars(
    eodhd,
    from_date: str,
    to_date: str,
) -> dict[str, list[dict]]:
    """Fetch EOD bars for all cross-asset symbols + S&P 500.

    Returns {symbol_key: [bar_dicts sorted by date]}.
    One API call per symbol.
    """
    from backend.cross_asset_stress import CROSS_ASSET_UNIVERSE

    # Build symbol map: key -> EODHD ticker
    symbols = {}
    for key, meta in CROSS_ASSET_UNIVERSE.items():
        symbols[key] = meta["symbol"]
    # Add S&P 500 for equity_return_1d
    symbols["SPX"] = "GSPC.INDX"

    results: dict[str, list[dict]] = {}
    for key, ticker in symbols.items():
        try:
            resp = eodhd.get_eod(ticker, from_date=from_date, to_date=to_date)
            bars = sorted(resp.rows, key=lambda b: str(b.get("date", "")))
            results[key] = bars
            LOG.info("  EODHD %s (%s): %d bars", key, ticker, len(bars))
        except Exception as e:
            LOG.warning("  EODHD %s (%s) failed: %s", key, ticker, e)
            results[key] = []
        time.sleep(0.15)  # gentle rate limiting

    return results


def _fetch_benzinga_headlines_for_day(benz, date_str: str) -> list[str]:
    """Fetch news headlines from Benzinga for a single day."""
    from backend.news_theme_intelligence import extract_headlines_from_benzinga

    try:
        resp = benz.news(date_from=date_str, date_to=date_str, page_size=100)
        headlines = extract_headlines_from_benzinga(resp.rows)
        return headlines
    except Exception as e:
        LOG.warning("  Benzinga news for %s failed: %s", date_str, e)
        return []


def _fetch_eodhd_headlines_for_day(eodhd, date_str: str) -> list[str]:
    """Fetch news headlines from EODHD for a single day."""
    from backend.news_theme_intelligence import extract_headlines_from_eodhd

    try:
        resp = eodhd.get_news(
            topic="market",
            from_date=date_str,
            to_date=date_str,
            limit=100,
        )
        headlines = extract_headlines_from_eodhd(resp.rows)
        return headlines
    except Exception as e:
        LOG.warning("  EODHD news for %s failed: %s", date_str, e)
        return []


def _fetch_calendar_events_bulk(
    benz,
    from_date: str,
    to_date: str,
) -> dict[str, list[dict]]:
    """Fetch economics + earnings calendar events and partition by date.

    Returns {date_str: [event_dicts]}.
    """
    events_by_date: dict[str, list[dict]] = {}

    # Economics calendar
    try:
        resp = benz.calendar_economics(date_from=from_date, date_to=to_date, pagesize=500)
        for ev in resp.rows:
            d = str(ev.get("date") or ev.get("date_time") or "")[:10]
            if d:
                events_by_date.setdefault(d, []).append(ev)
        LOG.info("  Benzinga economics calendar: %d events", len(resp.rows))
    except Exception as e:
        LOG.warning("  Benzinga economics calendar failed: %s", e)

    # Earnings calendar
    try:
        resp = benz.calendar_earnings(date_from=from_date, date_to=to_date, pagesize=500)
        for ev in resp.rows:
            d = str(ev.get("date") or ev.get("date_time") or "")[:10]
            if d:
                events_by_date.setdefault(d, []).append(ev)
        LOG.info("  Benzinga earnings calendar: %d events", len(resp.rows))
    except Exception as e:
        LOG.warning("  Benzinga earnings calendar failed: %s", e)

    return events_by_date


# ---------------------------------------------------------------------------
# Per-day processing
# ---------------------------------------------------------------------------


def _get_close(bars: list[dict], date_str: str) -> float | None:
    """Find close price for a specific date in a bar list."""
    for bar in bars:
        if str(bar.get("date", ""))[:10] == date_str:
            try:
                c = bar.get("adjusted_close") or bar.get("close")
                if c is not None:
                    return float(c)
            except (ValueError, TypeError):
                pass
    return None


def _get_prior_close(bars: list[dict], date_str: str) -> float | None:
    """Find the close for the trading day before date_str in the bar list."""
    prev = None
    for bar in sorted(bars, key=lambda b: str(b.get("date", ""))):
        bar_date = str(bar.get("date", ""))[:10]
        if bar_date >= date_str:
            break
        try:
            c = bar.get("adjusted_close") or bar.get("close")
            if c is not None:
                prev = float(c)
        except (ValueError, TypeError):
            pass
    return prev


def _history_closes_up_to(bars: list[dict], date_str: str) -> list[float]:
    """Get all closes up to and including date_str, sorted chronologically."""
    closes = []
    for bar in sorted(bars, key=lambda b: str(b.get("date", ""))):
        bar_date = str(bar.get("date", ""))[:10]
        if bar_date > date_str:
            break
        try:
            c = bar.get("adjusted_close") or bar.get("close")
            if c is not None:
                closes.append(float(c))
        except (ValueError, TypeError):
            pass
    return closes


def _compute_spx_return(bars: list[dict], date_str: str) -> float:
    """Compute 1-day S&P 500 return for a given date."""
    cur = _get_close(bars, date_str)
    prior = _get_prior_close(bars, date_str)
    if cur is not None and prior is not None and prior != 0:
        return round((cur - prior) / abs(prior) * 100, 4)
    return 0.0


# ---------------------------------------------------------------------------
# Main backfill logic
# ---------------------------------------------------------------------------


def main() -> int:
    from backend.eodhd_client import EodhdClient
    from backend.benzinga_client import BenzingaClient
    from backend.cross_asset_stress import (
        CROSS_ASSET_UNIVERSE,
        compute_asset_stress,
        build_cross_asset_snapshot,
    )
    from backend.news_theme_intelligence import (
        score_themes,
        persist_theme_snapshot,
    )
    from backend.daily_market_state import (
        build_daily_market_state,
        persist_dms,
        DailyMarketState,
    )
    from backend.front_layer_llm import detect_asymmetries
    from backend.redis_store import get_store_optional

    lookback = _parse_days_arg()
    LOG.info("=" * 60)
    LOG.info("Front Layer Historical Backfill – %d calendar days", lookback)
    LOG.info("=" * 60)

    # ── 0. Clients ─────────────────────────────────────────────────────
    try:
        eodhd = EodhdClient.from_env()
    except Exception as e:
        LOG.error("EODHD client init failed: %s", e)
        return 2

    try:
        benz = BenzingaClient.from_env()
    except Exception as e:
        LOG.warning("Benzinga client unavailable: %s (news themes will be empty)", e)
        benz = None

    store = get_store_optional()
    if not store:
        LOG.error("Redis not available. Backfill requires persistence. Exiting.")
        return 2

    # ── 1. Compute date range ──────────────────────────────────────────
    yesterday = dt.date.today() - dt.timedelta(days=1)
    start_date = yesterday - dt.timedelta(days=lookback - 1)
    # Extend bar fetch 5 extra days before start for prior_close / history
    bar_fetch_start = start_date - dt.timedelta(days=7)

    from_str = bar_fetch_start.isoformat()
    to_str = yesterday.isoformat()
    start_str = start_date.isoformat()

    trading_days = _trading_dates_in_range(start_date, yesterday)
    all_days = _calendar_dates_in_range(start_date, yesterday)

    LOG.info("Date range: %s to %s (%d trading days, %d calendar days)",
             start_str, to_str, len(trading_days), len(all_days))

    # ── 2. Fetch cross-asset bars ──────────────────────────────────────
    LOG.info("")
    LOG.info("Step 1/4: Fetching cross-asset EOD bars from EODHD...")
    all_bars = _fetch_cross_asset_bars(eodhd, from_str, to_str)

    api_calls = len(all_bars)
    total_bars = sum(len(v) for v in all_bars.values())
    LOG.info("  Total: %d symbols, %d bars, %d API calls", len(all_bars), total_bars, api_calls)

    # ── 3. Fetch calendar events in bulk ───────────────────────────────
    LOG.info("")
    LOG.info("Step 2/4: Fetching calendar events from Benzinga...")
    calendar_by_date: dict[str, list[dict]] = {}
    if benz:
        calendar_by_date = _fetch_calendar_events_bulk(benz, start_str, to_str)
        api_calls += 2
    else:
        LOG.warning("  Skipped (no Benzinga client)")

    # ── 4. Process day-by-day ──────────────────────────────────────────
    LOG.info("")
    LOG.info("Step 3/4: Processing trading days and building DMS snapshots...")
    LOG.info("")

    days_processed = 0
    days_failed = 0
    prior_theme_snapshots: list[dict] = []  # builds up chronologically

    for day in trading_days:
        day_str = day.isoformat()
        LOG.info("  ── %s ──", day_str)

        # --- Cross-asset stress ---
        spx_return = _compute_spx_return(all_bars.get("SPX", []), day_str)
        readings = []
        for key in CROSS_ASSET_UNIVERSE:
            bars = all_bars.get(key, [])
            cur = _get_close(bars, day_str)
            prior = _get_prior_close(bars, day_str)
            if cur is None or prior is None:
                continue
            history = _history_closes_up_to(bars, day_str)
            reading = compute_asset_stress(
                symbol_key=key,
                current_close=cur,
                prior_close=prior,
                equity_return_1d=spx_return,
                history_closes=history,
            )
            readings.append(reading)

        cross_asset_snap = None
        if readings:
            cross_asset_snap = build_cross_asset_snapshot(
                readings=readings,
                timestamp=f"{day_str}T16:00:00Z",
            )
            LOG.info("    Cross-asset: %d readings, composite=%.1f (%s)",
                     len(readings), cross_asset_snap.composite_score,
                     cross_asset_snap.composite_label)
        else:
            LOG.warning("    Cross-asset: no data for %s", day_str)

        # --- News themes ---
        headlines: list[str] = []
        if benz:
            headlines.extend(_fetch_benzinga_headlines_for_day(benz, day_str))
            api_calls += 1
            time.sleep(0.15)  # gentle rate limiting

        # Also try EODHD news
        headlines.extend(_fetch_eodhd_headlines_for_day(eodhd, day_str))
        api_calls += 1
        time.sleep(0.15)

        theme_snap = None
        themes_list: list[dict] = []
        if headlines:
            theme_snap = score_themes(
                headlines=headlines,
                prior_snapshots=prior_theme_snapshots,
                date_str=day_str,
            )
            themes_list = theme_snap.themes
            persist_theme_snapshot(theme_snap, store)
            # Add to rolling history for next day's persistence/acceleration calc
            prior_theme_snapshots.insert(0, theme_snap.to_dict())
            # Keep at most 14 days of history
            prior_theme_snapshots = prior_theme_snapshots[:14]
            LOG.info("    Themes: %d headlines, dominant=%s",
                     len(headlines), theme_snap.dominant_theme or "none")
        else:
            LOG.info("    Themes: no headlines available")

        # --- Calendar events ---
        day_events = calendar_by_date.get(day_str, [])
        event_count = len(day_events)
        high_sev = sum(
            1 for ev in day_events
            if str(ev.get("importance", "")).lower() in ("high", "critical", "3", "4", "5")
        )
        upcoming_titles = [
            str(ev.get("name") or ev.get("title") or "")
            for ev in day_events[:5]
            if ev.get("name") or ev.get("title")
        ]

        # --- Build DMS (regime/flow/sequencer unavailable for historical) ---
        dms = build_daily_market_state(
            date_str=day_str,
            regime=None,            # not available historically
            flow_pressure_snapshot=None,  # not available historically
            vol_direction="",
            iv_stress=50.0,
            event_count_5d=event_count,
            high_severity_count=high_sev,
            upcoming_events=upcoming_titles,
            cross_asset_stress=cross_asset_snap.to_dict() if cross_asset_snap else None,
            news_themes=themes_list,
            sequencer_summary=None,
        )

        # Override generated_at to reflect the actual historical date
        dms_dict = dms.to_dict()
        dms_dict["generated_at"] = f"{day_str}T08:55:00Z"
        dms_dict["_backfill"] = True

        # Detect asymmetries using history built so far
        # (limited for first few days, improves as we go)
        try:
            from backend.daily_market_state import load_dms_history
            history = load_dms_history(store, n=7)
            history_dicts = [h.to_dict() for h in history]
            asymmetries = detect_asymmetries(dms_dict, history_dicts)
            dms_dict["asymmetry_signals"] = asymmetries
            if asymmetries:
                LOG.info("    Asymmetries: %d signal(s)", len(asymmetries))
        except Exception as e:
            LOG.warning("    Asymmetry detection failed: %s", e)

        # Persist
        dms_final = DailyMarketState.from_dict(dms_dict)
        ok = persist_dms(dms_final, store)
        if ok:
            days_processed += 1
            LOG.info("    Persisted DMS for %s", day_str)
        else:
            days_failed += 1
            LOG.error("    FAILED to persist DMS for %s", day_str)

    # ── 5. Summary ─────────────────────────────────────────────────────
    LOG.info("")
    LOG.info("=" * 60)
    LOG.info("Backfill complete")
    LOG.info("  Trading days processed: %d", days_processed)
    LOG.info("  Failed:                 %d", days_failed)
    LOG.info("  API calls (approx):     %d", api_calls)
    LOG.info("  Date range:             %s to %s", start_str, to_str)
    LOG.info("=" * 60)

    # ── 6. Fetch headlines for weekend days too (themes only) ──────────
    weekend_days = [d for d in all_days if d.weekday() >= 5]
    if weekend_days and (benz or True):  # EODHD news available always
        LOG.info("")
        LOG.info("Step 4/4: Scoring weekend news themes (no DMS, themes only)...")
        for day in weekend_days:
            day_str = day.isoformat()
            headlines = []
            if benz:
                headlines.extend(_fetch_benzinga_headlines_for_day(benz, day_str))
                api_calls += 1
                time.sleep(0.15)
            headlines.extend(_fetch_eodhd_headlines_for_day(eodhd, day_str))
            api_calls += 1
            time.sleep(0.15)

            if headlines:
                theme_snap = score_themes(
                    headlines=headlines,
                    prior_snapshots=prior_theme_snapshots,
                    date_str=day_str,
                )
                persist_theme_snapshot(theme_snap, store)
                prior_theme_snapshots.insert(0, theme_snap.to_dict())
                prior_theme_snapshots = prior_theme_snapshots[:14]
                LOG.info("  %s (weekend): %d headlines, dominant=%s",
                         day_str, len(headlines), theme_snap.dominant_theme or "none")
            else:
                LOG.info("  %s (weekend): no headlines", day_str)
    else:
        LOG.info("")
        LOG.info("Step 4/4: No weekend days to process")

    LOG.info("")
    LOG.info("Done. Monday's cron and Market Intelligence page are ready.")
    return 1 if days_failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
