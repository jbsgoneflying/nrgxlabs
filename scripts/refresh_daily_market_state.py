#!/usr/bin/env python3
"""NRGX Labs Front Layer – Daily Market State Refresh Script (cron wrapper).

Builds and persists the DailyMarketState, generates Morning Brief, and
optionally generates the Weekly Roadmap (Sunday nights).

Schedule (crontab examples):
  # Daily at 03:55 EST
  55 3 * * * cd /path/to/Breach-Algo && python scripts/refresh_daily_market_state.py

  # Weekly roadmap – Sunday at 18:00 EST
  0 18 * * 0 cd /path/to/Breach-Algo && python scripts/refresh_daily_market_state.py --weekly

Usage:
    python scripts/refresh_daily_market_state.py [--weekly] [--force]

Exit codes:
    0 = success
    1 = partial (some engines failed but DMS was built)
    2 = fatal (DMS could not be built)
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys

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

LOG = logging.getLogger("refresh_dms")


def main() -> int:
    from backend.config import get_flags
    from backend.redis_store import get_store_optional
    from backend.daily_market_state import (
        build_daily_market_state, persist_dms, load_dms_history, DailyMarketState,
    )
    from backend.front_layer_llm import (
        generate_morning_brief, generate_weekly_roadmap, detect_asymmetries,
    )
    from backend.news_theme_intelligence import (
        score_themes, extract_headlines_from_eodhd, extract_headlines_from_benzinga,
        persist_theme_snapshot, load_theme_history,
    )

    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        LOG.info("Front Layer is disabled (ENABLE_FRONT_LAYER=false). Exiting.")
        return 0

    do_weekly = "--weekly" in sys.argv
    force = "--force" in sys.argv
    today_str = dt.date.today().isoformat()

    store = get_store_optional()
    if not store:
        LOG.error("Redis not available. DMS requires persistence. Exiting.")
        return 2

    partial = False  # Track if any engine failed

    # ── 1. Gather engine data ───────────────────────────────────────────

    # Engine 5: regime + vol
    regime_data = {}
    vol_direction = ""
    iv_stress = 50.0
    try:
        from backend.engine5_snapshot import select_best_snapshot
        snapshot = select_best_snapshot(store)
        if snapshot:
            snap_data = snapshot.get("data", {})
            regime_data = snap_data.get("regime", {})
            vol_ll = snap_data.get("volLeadLag", {})
            vol_direction = str(vol_ll.get("volLagState") or vol_ll.get("vol_lag_state", ""))
            iv_stress = float(regime_data.get("components", {}).get("iv_stress", 50.0))
            LOG.info("Engine 5 regime: %s (score %.1f)", regime_data.get("label", "?"), regime_data.get("score", 0))
        else:
            LOG.warning("Engine 5 snapshot not available")
            partial = True
    except Exception as e:
        LOG.warning("Engine 5 data unavailable: %s", e)
        partial = True

    # Sequencer
    seq_summary = {}
    try:
        from backend.sequencer import current_week_id, build_weekly_sequence, SequencerEvent
        wk = current_week_id()
        events_raw = store.get_json(f"sequencer:week:{wk}") or []
        events = [SequencerEvent.from_dict(e) for e in events_raw] if events_raw else []
        seq = build_weekly_sequence(week_id=wk, events=events)
        seq_summary = seq.to_dict()
        LOG.info("Sequencer: %d events, pattern=%s", len(events_raw), seq.pattern_match or "none")
    except Exception as e:
        LOG.warning("Sequencer data unavailable: %s", e)
        partial = True

    # Calendar / news risk
    event_count = 0
    high_sev = 0
    upcoming = []
    try:
        from backend.calendar_api import build_calendar_payload
        cal = build_calendar_payload(mode="week")
        events = cal.get("events", [])
        event_count = len(events)
        high_sev = sum(1 for ev in events if str(ev.get("importance", "")).lower() in ("high", "critical"))
        upcoming = [str(ev.get("title", "")) for ev in events[:5] if ev.get("title")]
        LOG.info("Calendar: %d events, %d high-severity", event_count, high_sev)
    except Exception as e:
        LOG.warning("Calendar data unavailable: %s", e)
        partial = True

    # News themes
    themes_list = []
    try:
        headlines = []
        try:
            from backend.eodhd_client import EodhdClient
            eodhd = EodhdClient.from_env()
            resp = eodhd.get_news(topic="market", limit=50)
            headlines.extend(extract_headlines_from_eodhd(resp.rows))
        except Exception as e:
            LOG.warning("EODHD news unavailable: %s", e)

        try:
            from backend.benzinga_client import BenzingaClient
            benz = BenzingaClient.from_env()
            resp = benz.news(page_size=50)
            headlines.extend(extract_headlines_from_benzinga(resp.rows))
        except Exception as e:
            LOG.warning("Benzinga news unavailable: %s", e)

        if headlines:
            prior_themes = load_theme_history(store, n_days=flags.FRONT_LAYER_THEME_LOOKBACK_DAYS)
            theme_snap = score_themes(headlines=headlines, prior_snapshots=prior_themes, date_str=today_str)
            themes_list = theme_snap.themes
            persist_theme_snapshot(theme_snap, store)
            LOG.info("Themes scored: %d headlines, dominant=%s", len(headlines), theme_snap.dominant_theme or "none")
        else:
            LOG.warning("No headlines available for theme scoring")
            partial = True
    except Exception as e:
        LOG.warning("Theme scoring failed: %s", e)
        partial = True

    # Cross-asset stress
    cross_asset_snap = None
    try:
        from backend.eodhd_client import EodhdClient as _EodhdCls
        from backend.cross_asset_stress import (
            CROSS_ASSET_UNIVERSE, compute_asset_stress,
            build_cross_asset_snapshot, AssetStressReading,
        )
        _eodhd = _EodhdCls.from_env()
        spx_return = 0.0
        try:
            spx_resp = _eodhd.get_eod("GSPC.INDX", period="d")
            spx_bars = sorted(spx_resp.rows, key=lambda b: str(b.get("date", "")))
            if len(spx_bars) >= 2:
                cur_c = float(spx_bars[-1].get("adjusted_close") or spx_bars[-1].get("close", 0))
                prv_c = float(spx_bars[-2].get("adjusted_close") or spx_bars[-2].get("close", 0))
                if prv_c:
                    spx_return = round((cur_c - prv_c) / abs(prv_c) * 100, 4)
        except Exception:
            pass

        readings = []
        for key, meta in CROSS_ASSET_UNIVERSE.items():
            try:
                resp = _eodhd.get_eod(meta["symbol"], period="d")
                bars = sorted(resp.rows, key=lambda b: str(b.get("date", "")))
                if len(bars) >= 2:
                    cur_c = float(bars[-1].get("adjusted_close") or bars[-1].get("close", 0))
                    prv_c = float(bars[-2].get("adjusted_close") or bars[-2].get("close", 0))
                    history = [float(b.get("adjusted_close") or b.get("close", 0)) for b in bars[-30:]]
                    r = compute_asset_stress(
                        symbol_key=key,
                        current_close=cur_c,
                        prior_close=prv_c,
                        equity_return_1d=spx_return,
                        history_closes=history,
                    )
                    readings.append(r)
            except Exception:
                pass

        if readings:
            import datetime as _dt
            now_ts = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
            cross_asset_snap = build_cross_asset_snapshot(
                readings=readings,
                timestamp=now_ts,
            ).to_dict()
            LOG.info("Cross-asset stress: %d readings, composite=%.1f",
                     len(readings), cross_asset_snap.get("composite_score", 0))
    except Exception as e:
        LOG.warning("Cross-asset stress unavailable: %s", e)
        partial = True

    # ── 2. Build DailyMarketState ──────────────────────────────────────

    dms = build_daily_market_state(
        date_str=today_str,
        regime=regime_data,
        vol_direction=vol_direction,
        iv_stress=iv_stress,
        event_count_5d=event_count,
        high_severity_count=high_sev,
        upcoming_events=upcoming,
        cross_asset_stress=cross_asset_snap,
        news_themes=themes_list,
        sequencer_summary=seq_summary,
    )

    # Detect asymmetries
    dms_dict = dms.to_dict()
    history = load_dms_history(store, n=flags.FRONT_LAYER_DMS_HISTORY_DAYS)
    history_dicts = [h.to_dict() for h in history]
    asymmetries = detect_asymmetries(dms_dict, history_dicts)
    dms_dict["asymmetry_signals"] = asymmetries

    # Persist
    dms_final = DailyMarketState.from_dict(dms_dict)
    ok = persist_dms(dms_final, store, ttl_s=flags.FRONT_LAYER_DMS_TTL_S)
    if not ok:
        LOG.error("Failed to persist DMS")
        return 2

    LOG.info("DailyMarketState built and persisted for %s", today_str)
    if asymmetries:
        LOG.info("Asymmetry radar: %d signal(s) detected", len(asymmetries))

    # ── 3. Generate Morning Brief ──────────────────────────────────────

    if flags.ENABLE_FRONT_LAYER_LLM:
        try:
            brief = generate_morning_brief(dms_dict, history_dicts)
            store.set_json(f"front_layer:brief:{today_str}", brief, ttl_s=7 * 86400)
            LOG.info("Morning Brief generated (source=%s)", brief.get("_source", "?"))
        except Exception as e:
            LOG.warning("Morning Brief generation failed: %s", e)
            partial = True
    else:
        LOG.info("LLM generation disabled; skipping Morning Brief")

    # ── 4. Generate Weekly Roadmap (Sunday only) ───────────────────────

    if do_weekly and flags.ENABLE_FRONT_LAYER_LLM:
        try:
            roadmap = generate_weekly_roadmap(dms_dict, history_dicts)
            store.set_json(f"front_layer:roadmap:{today_str}", roadmap, ttl_s=7 * 86400)
            LOG.info("Weekly Roadmap generated (source=%s)", roadmap.get("_source", "?"))
        except Exception as e:
            LOG.warning("Weekly Roadmap generation failed: %s", e)
            partial = True
    elif do_weekly:
        LOG.info("LLM generation disabled; skipping Weekly Roadmap")

    return 1 if partial else 0


if __name__ == "__main__":
    raise SystemExit(main())
