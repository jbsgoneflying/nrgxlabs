from __future__ import annotations

import datetime as dt
import logging
import math
import statistics
import time
from typing import Any, Dict, List, Optional, Tuple

from backend.benzinga_client import BenzingaClient
from backend.config import FeatureFlags
from backend.dealer_gamma_context import compute_dealer_gamma_context
from backend.engine2_gamma_addons import (
    compute_hedging_pressure,
    compute_tail_ignition,
    compute_vol_pressure,
)
from backend.expected_move import compute_expected_move_from_chain, compute_strike_targets
from backend.oi_clusters import compute_open_interest_clusters
from backend.orats_client import OratsClient, OratsError
from backend.e2_history_breaker import compute_e2_history_breaker_risk
from backend.technicals import DailyBar as TechDailyBar
from backend.technicals import (
    _ema_series,
    build_ta_narrative,
    build_ta_signals,
    compute_bollinger_series,
    compute_distances,
    compute_ema_levels,
    compute_ichimoku_levels,
    compute_macd_series,
    compute_rsi_series,
    compute_vwap_proxy,
    detect_candlestick_patterns,
    detect_elliott_pivot_structure,
    detect_red_dog_reversal,
    fetch_live_price_context_optional,
)

from backend.spx_ic.utils import (
    _fmt_date,
    _parse_date,
    _now_et,
    _after_cash_close_et,
    _pick_nearest_expiry_date,
    _pick_weekly_close_expiry_date,
    _to_float,
    _iv_to_pct,
    _pick_spot_from_live_rows,
)


def _regime_score_value(regime_payload: Dict[str, Any], default: float = 50.0) -> float:
    """Pull the 0-100 regime score out of ``compute_regime_score_for_date`` output.

    The regime payload only exposes the field as ``score100``; older call sites
    read ``score`` (never emitted) and silently fell back to ``default`` → the
    desk-consensus / EM-preference layers were always running on a neutral 50.
    Accept either key so we can't regress again.
    """
    if not isinstance(regime_payload, dict):
        return default
    for key in ("score100", "score"):
        raw = regime_payload.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return default
from backend.spx_ic.ohlc import (
    DailyOHLC,
    fetch_dailies_ohlc_range,
    fetch_hist_cores_range,
    fetch_trading_bars,
    iv_to_em1sigma_pct,
)
from backend.spx_ic.regime import (
    compute_regime_score_for_date,
    compute_sector_dispersion_series,
    _macro_context,
)
from backend.spx_ic.weekly_windows import (
    WeeklyWindow,
    build_weekly_windows_from_trade_dates,
)
from backend.spx_ic.backtest import (
    backtest_weekly_ic_risk,
    recommend_width,
    beta_binomial_mean,
    pctile,
)
from backend.spx_ic.live_levels import (
    compute_live_levels,
    compute_spx_live_levels,
    compute_expected_move_weekly,
)

from backend.spx_ic.utils import _pct_ret, _quarter_key
from backend.spx_ic.regime import (
    _log_returns,
    _parkinson_vol,
    _is_summer,
    _is_opex_week,
    _prefetch_benzinga_economics,
)
from backend.spx_ic.live_levels import (
    _infer_live_expiries_from_strikes,
    _filter_chain_by_expiry,
    _live_chain_with_fallback,
    _compute_gamma_flip_strike,
)

LOG = logging.getLogger("spx_ic_engine")


# ---------------------------------------------------------------------------
# EM preference scorer (Engine12-style composite)
# ---------------------------------------------------------------------------

def _clamp(lo: float, hi: float, v: float) -> float:
    return max(lo, min(hi, v))


def _compute_em_preference(
    *,
    regime_score: float,
    macro_multiplier: float,
    news_gate_max_adj: float = 0.0,
    vol_pressure_state: str = "NEUTRAL",
    dealer_gamma_sign: str = "unknown",
) -> Dict[str, Any]:
    """Regime-adaptive EM multiplier selection.

    Normalises regime, macro, news-gate, and vol-pressure into 0-100 subscores
    (higher = more conservative market), applies a weighted composite with a
    dealer-gamma modifier and a multi-risk stacking amplifier, then maps
    bands to an EM preference.

    The stacking amplifier addresses the weakness of weighted averages: when
    multiple independent risk factors fire simultaneously, the combined danger
    compounds — calm factors should not dilute that signal.

    Returns dict with emPreference (1.0 / 1.5 / 2.0), label, compositeScore,
    and per-factor breakdown for transparency.
    """
    regime_f = _clamp(0, 100, float(regime_score))
    macro_f = _clamp(0, 100, (float(macro_multiplier) - 1.0) * 100.0)
    news_f = _clamp(0, 100, float(news_gate_max_adj))
    vol_map = {"BID": 60.0, "NEUTRAL": 30.0, "ASK": 10.0}
    vol_f = vol_map.get(str(vol_pressure_state).upper(), 30.0)

    raw = (
        regime_f * 0.35
        + macro_f * 0.25
        + news_f * 0.20
        + vol_f * 0.20
    )

    gamma_adj = 0.0
    gs = str(dealer_gamma_sign).lower()
    if gs == "negative":
        gamma_adj = 8.0
    elif gs == "positive":
        gamma_adj = -5.0

    caution_flags = 0
    if regime_f > 45:
        caution_flags += 1
    if macro_f > 70:
        caution_flags += 1
    if news_f > 30:
        caution_flags += 1
    if vol_f >= 60:
        caution_flags += 1
    if gs == "negative":
        caution_flags += 1

    stacking_bonus = max(0.0, (caution_flags - 2)) * 10.0

    score = _clamp(0, 100, raw + gamma_adj + stacking_bonus)

    if score < 35:
        em_pref, label = 1.0, "aggressive"
    elif score < 60:
        em_pref, label = 1.5, "standard"
    else:
        em_pref, label = 2.0, "defensive"

    return {
        "emPreference": em_pref,
        "label": label,
        "compositeScore": round(score, 1),
        "components": {
            "regime": round(regime_f, 1),
            "macro": round(macro_f, 1),
            "newsGate": round(news_f, 1),
            "volPressure": round(vol_f, 1),
            "gammaAdj": round(gamma_adj, 1),
            "stackingBonus": round(stacking_bonus, 1),
            "cautionFlags": caution_flags,
        },
        "weights": {"regime": 0.35, "macro": 0.25, "newsGate": 0.20, "volPressure": 0.20},
    }


def _em_fallback_order(preferred: float, available: List[float]) -> List[float]:
    """Return EM multiples in proximity order from the preferred value."""
    others = [e for e in sorted(available) if abs(e - preferred) > 1e-9]
    others.sort(key=lambda e: abs(e - preferred))
    return others


def compute_engine2_spx_ic(
    *,
    client: OratsClient,
    benzinga_client: Optional[BenzingaClient],
    flags: FeatureFlags,
    underlying_preference: str = "SPX",  # SPX|SPY|QQQ
    entry_day: str = "mon",
    years: int = 3,
    widths: Optional[List[float]] = None,
    risk_target_breach_pct: float = 25.0,
    seasonality_mode: str = "none",  # none|quarter|month|summer|opex
    today: Optional[dt.date] = None,
) -> Dict[str, Any]:
    """
    Main Engine 2 payload generator.
    Uses SPY as the default proxy for SPX if SPX is not available in ORATS dailies.
    """
    t0 = time.perf_counter()
    telemetry: Dict[str, Any] = {"timingsMs": {}, "counts": {}, "notes": []}

    def mark(name: str) -> None:
        telemetry["timingsMs"][name] = int(round((time.perf_counter() - t0) * 1000.0))

    def add_count(name: str, delta: int = 1) -> None:
        telemetry["counts"][name] = int(telemetry["counts"].get(name, 0)) + int(delta)

    # v2: desk defaults preserved, but `years` / `widths` query params are
    # now respected when provided (previously silently overwritten to 2y
    # + [1.0, 1.5, 2.0]).  Defaults still give today's scan the same
    # shape so the desk's muscle memory stays intact.
    #
    # - 2y lookback (~104 weekly observations per entry weekday) — default
    # - widths 1.0/1.5/2.0 × EM (short distance)                  — default
    # - wings: $5 only (legacy) or multi-width when ENGINE2_MULTI_WING is enabled
    try:
        yrs_req = int(years) if years is not None else 0
    except Exception:
        yrs_req = 0
    yrs = yrs_req if (yrs_req and 1 <= yrs_req <= 10) else 2
    widths_use: List[float] = [1.0, 1.5, 2.0]
    if widths:
        try:
            # Accept list[float] (native call-site) or comma-separated str.
            if isinstance(widths, (list, tuple)):
                _parsed = [float(x) for x in widths]
            else:
                _parsed = [float(x.strip()) for x in str(widths).split(",") if x.strip()]
            _parsed = [w for w in _parsed if 0.1 <= w <= 5.0]
            if _parsed:
                widths_use = sorted(set(_parsed))
        except Exception:
            widths_use = [1.0, 1.5, 2.0]
    em_mults = list(widths_use)
    if flags.ENGINE2_MULTI_WING:
        _raw_wp = [p.strip() for p in str(flags.ENGINE2_WING_WIDTH_PTS).split(",") if p.strip()]
        wing_pts = sorted({int(float(p)) for p in _raw_wp if float(p) > 0}) or [5]
    else:
        wing_pts = [5]
    ed = str(entry_day or "mon").strip().lower()
    entry_dow = 0 if ed.startswith("mon") else 1 if ed.startswith("tue") else 2 if ed.startswith("wed") else 0
    now = today or dt.date.today()
    # Use an explicit, timezone-aware "now" for live expiry roll logic.
    # (Weekly expiry rolls after 4:15pm ET on Fridays.)
    now_dt_utc = dt.datetime.now(dt.timezone.utc)
    season_mode = str(seasonality_mode or "none").strip().lower()
    LOG.info("Engine2 compute start (desk-locked): entry_day=%s years=%s widths=%s wingPts=%s seasonality=%s", ed, yrs, widths_use, wing_pts, season_mode)

    def _season_bucket(d: dt.date) -> str:
        if season_mode == "quarter":
            return _quarter_key(d)
        if season_mode == "month":
            return f"M{int(d.month):02d}"
        if season_mode == "summer":
            return "SUMMER" if _is_summer(d) else "NON_SUMMER"
        if season_mode == "opex":
            return "OPEX" if _is_opex_week(d) else "NON_OPEX"
        return "ALL"

    # Ticker selection:
    # - If preference=SPX: prefer SPX, fallback to SPY proxy if SPX dailies unavailable.
    # - If preference=SPY: prefer SPY, fallback to SPX proxy if SPY dailies unavailable (explicitly noted).
    proxy_notes: List[str] = []
    pref = str(underlying_preference or "SPX").strip().upper()
    if pref not in ("SPX", "SPY", "QQQ"):
        pref = "SPX"
        proxy_notes.append("Invalid underlying preference; defaulted to SPX.")

    # Underlying selection policy:
    # - Prefer the requested underlying.
    # - For SPX<->SPY only: allow a proxy fallback if the preferred ticker is unavailable in ORATS dailies.
    # - For QQQ: do not proxy.
    underlying = pref
    is_proxy = False

    # Use range probe (fast + consistent) to detect availability.
    probe_rows = fetch_dailies_ohlc_range(client, ticker=underlying, start=now - dt.timedelta(days=7), end=now)
    telemetry["counts"]["orats.probe_rows"] = len(probe_rows or [])
    if not probe_rows and pref in ("SPX", "SPY"):
        alt = "SPY" if pref == "SPX" else "SPX"
        probe_rows_alt = fetch_dailies_ohlc_range(client, ticker=alt, start=now - dt.timedelta(days=7), end=now)
        if probe_rows_alt:
            underlying = alt
            is_proxy = True
            proxy_notes.append(f"{pref} unavailable in ORATS dailies; using {alt} as a proxy for this run.")
            probe_rows = probe_rows_alt
            telemetry["counts"]["orats.probe_rows"] = len(probe_rows or [])
    if not probe_rows:
        raise OratsError(f"{underlying} unavailable in ORATS dailies (no rows returned for probe window).")

    # Build OHLC history once (range pull; fast).
    start_hist = now - dt.timedelta(days=int(yrs) * 365 + 120)
    bars = fetch_dailies_ohlc_range(client, ticker=underlying, start=start_hist, end=now)
    mark("orats.dailies_range")
    if not bars:
        # Fail safe: old slow path (should rarely happen)
        bars = fetch_trading_bars(client, ticker=underlying, end=now, n=1100, max_calendar_scan=1600)
        mark("orats.dailies_fallback_slow")
    trade_dates = [b.trade_date for b in bars]
    bar_by_date: Dict[str, DailyOHLC] = {b.trade_date: b for b in bars if b and b.trade_date}
    idx_by_date: Dict[str, int] = {b.trade_date: i for i, b in enumerate(bars) if b and b.trade_date}
    closes = [float(b.close) for b in bars if b.close is not None]
    logrets_all = _log_returns(closes)
    telemetry["counts"]["orats.dailies_rows"] = len(bars)
    telemetry["counts"]["trade_dates"] = len(trade_dates)

    # Build weekly windows for backtest (fast: derived from already-fetched trade_dates).
    windows = build_weekly_windows_from_trade_dates(
        trade_dates=trade_dates,
        start=(now - dt.timedelta(days=yrs * 365)),
        end=now,
        entry_dow=entry_dow,
        max_weeks=260 * yrs,
    )
    telemetry["counts"]["windows"] = len(windows)
    mark("build.windows")

    # IV samples are optional; in rate-limited environments we avoid per-week surface loads.
    iv_weekly_sample: Dict[str, Dict[str, float]] = {}
    # Per-week macro context (if Benzinga available)
    macro_by_entry: Dict[str, Dict[str, Any]] = {}

    # Batch fetch Benzinga economics once for the whole backtest span (avoid N network calls).
    econ_by_date: Dict[str, List[dict]] = {}
    if benzinga_client is not None:
        try:
            if windows:
                # IMPORTANT: ORATS EOD can lag during market hours, so the last backtest window may end
                # before the upcoming "next week" macro window. Ensure the prefetch also covers forward
                # dates from 'now' so the current macro panel is populated.
                econ_start = min(windows[0].entry_date - dt.timedelta(days=7), now - dt.timedelta(days=30))
                econ_end = max(windows[-1].expiry_date + dt.timedelta(days=7), now + dt.timedelta(days=21))
            else:
                econ_start = now - dt.timedelta(days=30)
                econ_end = now + dt.timedelta(days=21)
            # Fetch only the slice we actually use for the macro overlay: US + high-impact items.
            # This avoids huge pagination ranges that can omit recent dates depending on API ordering.
            econ_rows_all = _prefetch_benzinga_economics(
                benzinga_client,
                start=econ_start,
                end=econ_end,
                pagesize=1000,
                max_pages=8,
                importance=3,
                country="US",
            )
            telemetry["counts"]["benzinga.econ_rows"] = len(econ_rows_all)
            for r in econ_rows_all:
                d0 = str(r.get("date") or "")[:10]
                if not d0:
                    continue
                econ_by_date.setdefault(d0, []).append(r)
        except Exception:
            econ_by_date = {}
            telemetry["notes"].append("Benzinga economics prefetch failed (non-fatal).")
    mark("benzinga.economics_prefetch")

    # Batch fetch ORATS IV series via /hist/cores (fast, supports fromDate/toDate).
    # This avoids 100+ slow /hist/monies/implied calls when range mode isn't supported there.
    iv7_by_date: Dict[str, float] = {}
    iv30_by_date: Dict[str, float] = {}
    slope_by_date: Dict[str, float] = {}
    try:
        from_core = (now - dt.timedelta(days=int(yrs) * 365 + 120))
        to_core = now
        fields = "ticker,tradeDate,iv7,iv7d,iv7Day,iv30,iv30d,iv30Day,iv,slope"
        core_rows = fetch_hist_cores_range(client, ticker=underlying, start=from_core, end=to_core, fields=fields)
        telemetry["counts"]["orats.cores_rows"] = len(core_rows)
        for r in core_rows:
            d0 = str(r.get("tradeDate") or "")[:10]
            if not d0:
                continue
            iv7 = None
            for k in ("iv7", "iv7d", "iv7Day"):
                iv7 = _iv_to_pct(r.get(k))
                if iv7 is not None:
                    break
            iv30 = None
            for k in ("iv30", "iv30d", "iv30Day", "iv"):
                iv30 = _iv_to_pct(r.get(k))
                if iv30 is not None:
                    break
            if iv7 is not None:
                iv7_by_date[d0] = float(iv7)
            if iv30 is not None:
                iv30_by_date[d0] = float(iv30)
            s0 = _to_float(r.get("slope"))
            if s0 is not None:
                slope_by_date[d0] = float(s0)
    except Exception:
        telemetry["notes"].append("ORATS cores IV range fetch failed; IV inputs will be reduced (fallback to realized vol).")
        iv7_by_date = {}
        iv30_by_date = {}
        slope_by_date = {}
    mark("orats.cores_iv_range")

    # Realized vol proxy: 10d annualized stdev of log returns (percent)
    rv10_by_date: Dict[str, float] = {}
    try:
        # logrets_all aligns with trade_dates[1:]
        for i in range(1, len(trade_dates)):
            if i - 10 < 0:
                continue
            window_rets = [float(x) for x in logrets_all[i - 10 : i] if x is not None and math.isfinite(float(x))]
            if len(window_rets) < 6:
                continue
            try:
                sd = float(statistics.pstdev(window_rets))
            except Exception:
                sd = None
            if sd is None or not math.isfinite(sd) or sd <= 0:
                continue
            rv = float(sd) * math.sqrt(252.0) * 100.0
            rv10_by_date[str(trade_dates[i])[:10]] = float(rv)
    except Exception:
        rv10_by_date = {}

    # ADV proxy (shares): 20d average daily volume from ORATS dailies (best-effort)
    adv20_shares = None
    try:
        vols = [float(b.volume) for b in (bars or []) if getattr(b, "volume", None) is not None and float(b.volume) > 0]
        if len(vols) >= 5:
            tail = vols[-20:] if len(vols) >= 20 else vols
            adv20_shares = float(sum(tail) / len(tail)) if tail else None
    except Exception:
        adv20_shares = None

    # Precompute sector dispersion (EOD) across trade_dates.
    sector_tickers = ["XLF", "XLK", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU"]
    sector_disp = compute_sector_dispersion_series(client, dates=trade_dates, sector_tickers=sector_tickers)
    telemetry["counts"]["orats.sector_tickers"] = len(sector_tickers)
    telemetry["counts"]["sector_dispersion_dates"] = len(sector_disp)
    mark("orats.sector_dispersion")

    # Collect week records and grid aggregations.
    week_rows: List[Dict[str, Any]] = []
    # Key: (entryDay, regimeBucket, macroBucket, emMult, wingPts)
    agg: Dict[Tuple[str, str, str, str, float, int], Dict[str, Any]] = {}

    def _macro_bucket(m: Dict[str, Any]) -> str:
        try:
            mult = float(m.get("multiplier") or 1.0)
        except Exception:
            mult = 1.0
        flags0 = m.get("flags") if isinstance(m.get("flags"), dict) else {}
        hi = any(bool(flags0.get(k)) for k in ("CPI", "FOMC", "NFP"))
        return "MACRO" if (mult >= 1.25 or hi) else "NORMAL"

    for win in windows:
        entry = win.entry_date
        expiry = win.expiry_date
        ek = _fmt_date(entry)
        fk = _fmt_date(expiry)
        entry_bar = bar_by_date.get(ek)
        exp_bar = bar_by_date.get(fk)
        if not entry_bar or not exp_bar or entry_bar.close is None or exp_bar.close is None or entry_bar.close <= 0:
            continue

        entry_px = float(entry_bar.close)
        exp_px = float(exp_bar.close)
        ret_pct = _pct_ret(entry_px, exp_px)

        # Weekly EM(1σ) using ORATS cores IV series (fast). Prefer iv7 for weekly horizons.
        dte_h = max(1, int(win.dte_calendar_days))
        iv7 = iv7_by_date.get(ek)
        iv30 = iv30_by_date.get(ek)
        iv_h = iv7 if iv7 is not None else iv30
        if iv_h is None or float(iv_h) <= 0:
            # Last resort: realized-vol proxy (keeps engine alive on missing IV rows)
            i0 = idx_by_date.get(ek)
            vol_ann = None
            if i0 is not None and i0 >= 3:
                lr = logrets_all[:i0]
                w = min(20, len(lr))
                if w >= 2:
                    try:
                        vol_ann = statistics.stdev(lr[-w:]) * math.sqrt(252.0)
                    except Exception:
                        vol_ann = None
            if vol_ann is None:
                vol_ann = _parkinson_vol(bars[: (i0 + 1)] if i0 is not None else bars)
            if vol_ann is None or float(vol_ann) <= 0:
                continue
            em1sigma_pct = float(vol_ann) * 100.0 * math.sqrt(max(1, int(win.dte_sessions)) / 252.0)
            em_source = "RV20"
        else:
            em1sigma_pct = iv_to_em1sigma_pct(iv_pct=float(iv_h), dte_calendar_days=max(1, int(win.dte_calendar_days)))
            em_source = "IV"
            # Cache implied samples for regime scoring (term slope / vv).
            iv_weekly_sample[ek] = {
                "iv7": float(iv7) if iv7 is not None else float(iv_h),
                "iv30": float(iv30) if iv30 is not None else float(iv_h),
            }

        # Macro context for the week (Mon..Fri) anchored to entry
        macro = None
        if benzinga_client is not None:
            # Week window is entry-week Monday -> Friday
            mon = entry - dt.timedelta(days=entry.weekday())
            fri = mon + dt.timedelta(days=4)
            # Use pre-fetched economics rows to avoid repeated network calls.
            econ_rows_week: List[dict] = []
            d0 = mon
            while d0 <= fri:
                econ_rows_week.extend(econ_by_date.get(_fmt_date(d0), []))
                d0 += dt.timedelta(days=1)
            macro = _macro_context(benzinga_client, start=mon, end=fri, as_of=entry, flags=flags, economics_rows=econ_rows_week)
        if macro is None:
            macro = {"multiplier": 1.0, "flags": {"OPEX": bool(_is_opex_week(expiry))}, "highImpactUS": {"count": 0, "top": []}, "notes": ["Benzinga unavailable or disabled."]}
        macro_by_entry[_fmt_date(entry)] = macro

        # Regime at entry (0..100)
        r = compute_regime_score_for_date(
            client,
            ticker=underlying,
            as_of=entry,
            bars=bars,
            flags=flags,
            iv_weekly_sample=(iv_weekly_sample if iv_weekly_sample else None),
            sector_dispersion_cache=sector_disp,
            macro_multiplier=float(macro.get("multiplier") or 1.0),
            macro_flags=(macro.get("flags") if isinstance(macro.get("flags"), dict) else None),
        )
        bucket = str(r.get("bucket") or "MODERATE")
        mb = _macro_bucket(macro)

        # MAE/MFE (absolute, points)
        mae_abs_pct = 0.0
        up_mae_pct = 0.0
        down_mae_pct = 0.0
        # Use the already-fetched bars (no per-day ORATS calls).
        i0 = idx_by_date.get(ek)
        i1 = idx_by_date.get(fk)
        if i0 is not None and i1 is not None and i1 >= i0:
            for b in bars[i0 : i1 + 1]:
                if b.high is not None and b.low is not None:
                    up_mae_pct = max(up_mae_pct, (float(b.high) / entry_px - 1.0) * 100.0)
                    down_mae_pct = max(down_mae_pct, (1.0 - float(b.low) / entry_px) * 100.0)
        mae_abs_pct = max(up_mae_pct, down_mae_pct)
        mae_abs_pts = mae_abs_pct / 100.0 * entry_px
        mae_abs_em = mae_abs_pct / float(em1sigma_pct) if em1sigma_pct > 1e-9 else None

        # Seasonality labels
        season = {
            "quarter": _quarter_key(entry),
            "month": int(entry.month),
            "isSummer": bool(_is_summer(entry)),
            "isOpexWeek": bool(_is_opex_week(expiry)),
        }
        season_bucket = _season_bucket(entry)

        week_rows.append(
            {
                "entryDate": _fmt_date(entry),
                "expiryDate": _fmt_date(expiry),
                "dte": int(win.dte_sessions),
                "entryPx": round(entry_px, 2),
                "expiryPx": round(exp_px, 2),
                "retPct": round(float(ret_pct), 3),
                "em1sigmaPct": round(float(em1sigma_pct), 3),
                "emSource": em_source,
                "macroMultiplier": round(float(macro.get("multiplier") or 1.0), 3),
                "regimeScore100": float(r.get("score100") or 50.0),
                "regimeBucket": bucket,
                "macroBucket": mb,
                "seasonBucket": season_bucket,
                "maeAbsPts": round(float(mae_abs_pts), 2),
                "maeAbsEm": None if mae_abs_em is None else round(float(mae_abs_em), 3),
                "seasonality": season,
            }
        )

        # Aggregate grid over EM multiples and wing widths
        diff_pts = abs(exp_px - entry_px)
        for em in em_mults:
            if em <= 0:
                continue
            short_dist_pts = (float(em) * float(em1sigma_pct) / 100.0) * entry_px
            breach = diff_pts > short_dist_pts
            for wp in wing_pts:
                if int(wp) <= 0:
                    continue
                long_dist_pts = short_dist_pts + float(wp)
                outside = diff_pts > long_dist_pts
                k = (ed, bucket, mb, season_bucket, float(em), int(wp))
                cell = agg.get(k)
                if cell is None:
                    cell = {"n": 0, "breach": 0, "outside": 0, "maePts": [], "lossPts": []}
                    agg[k] = cell
                cell["n"] += 1
                cell["breach"] += 1 if breach else 0
                cell["outside"] += 1 if outside else 0
                cell["maePts"].append(float(mae_abs_pts))
                # Worst-case expiry loss proxy (no credit): intrinsic loss beyond short strikes, capped by wing width.
                loss_pts = max(0.0, float(diff_pts) - float(short_dist_pts))
                loss_pts = min(float(wp), loss_pts)
                cell["lossPts"].append(float(loss_pts))

    # Current macro context (for recommendation)
    # Rolling window (requested): today .. today+7 (ET), not limited to Mon..Fri.
    macro_now = None
    if benzinga_client is not None:
        d0 = now
        exp0 = now + dt.timedelta(days=7)
        econ_rows_now: List[dict] = []
        d1 = d0
        while d1 <= exp0:
            econ_rows_now.extend(econ_by_date.get(_fmt_date(d1), []))
            d1 += dt.timedelta(days=1)
        macro_now = _macro_context(benzinga_client, start=d0, end=exp0, as_of=now, flags=flags, economics_rows=econ_rows_now)
    if macro_now is None:
        macro_now = {"multiplier": 1.0, "flags": {"OPEX": bool(_is_opex_week(now + dt.timedelta(days=7)))}, "highImpactUS": {"count": 0, "top": []}, "notes": ["Benzinga unavailable or disabled."]}
    macro_bucket_now = _macro_bucket(macro_now)
    regime_now = compute_regime_score_for_date(
        client,
        ticker=underlying,
        as_of=now,
        bars=bars,
        flags=flags,
        iv_weekly_sample=(iv_weekly_sample if iv_weekly_sample else None),
        sector_dispersion_cache=sector_disp,
        macro_multiplier=float(macro_now.get("multiplier") or 1.0),
        macro_flags=(macro_now.get("flags") if isinstance(macro_now.get("flags"), dict) else None),
    )
    regime_bucket_now = str(regime_now.get("bucket") or "MODERATE")
    season_bucket_now = _season_bucket(now)

    # --- Live options context (current-only, informational) ---
    live_context: Dict[str, Any] = {
        "enabled": False,
        # Backwards-compatible "primary" view fields (we set these to weeklyFriday if available).
        "symbolUsed": None,
        "expiry": None,
        "spot": None,
        "bandPct": 0.05,
        "atmIvPct": None,
        "greeksAgg": None,
        "dealerGamma": None,
        "oiClusters": None,
        # New: dual live views
        "weeklyFriday": None,
        "nearestDaily": None,
        "warnings": [],
        "notes": ["Live context unavailable."],
    }
    try:
        # Only attempt if live methods exist (keeps unit tests/mock clients safe).
        if callable(getattr(client, "live_strikes_by_expiry", None)) and callable(getattr(client, "live_strikes", None)):
            # Build expiries list once per symbol.
            # Do NOT hard depend on /live/expirations since some entitlements return empty expirations;
            # infer expiries from full strikes as fallback.
            exp_warn: List[str] = []
            strikes_cache_by_symbol: Dict[str, List[dict]] = {}
            exp_dates_by_symbol: Dict[str, List[str]] = {}

            # Respect user's Engine2 underlying selection (no cross-ticker proxy):
            # - SPX: allow SPXW -> SPX (same family), but never SPY
            # - SPY: SPY only
            # - QQQ: QQQ only
            symbols = ("SPXW", "SPX") if pref == "SPX" else (pref,)
            fields0 = "ticker,tradeDate,expirDate,expiry,expDate,exp_date,strike,spotPrice,stockPrice,gamma,theta,vega,callOpenInterest,putOpenInterest,callVolume,putVolume,callMidIv,putMidIv"

            for sym in symbols:
                exp_dates: List[str] = []
                exp_rows: List[dict] = []
                try:
                    if callable(getattr(client, "live_expirations", None)):
                        exp_rows = client.live_expirations(ticker=sym).rows or []
                except Exception as e:
                    exp_warn.append(f"Live expirations error for {sym}: {type(e).__name__}: {e}")
                    exp_rows = []

                if exp_rows:
                    for r in exp_rows:
                        if not isinstance(r, dict):
                            continue
                        d0 = str(r.get("expirDate") or r.get("expiry") or r.get("expDate") or r.get("exp_date") or "")[:10]
                        if d0 and len(d0) >= 10:
                            exp_dates.append(d0)
                else:
                    # Fallback: infer expiries from full strikes payload (cached short-TTL).
                    try:
                        all_rows = client.live_strikes(ticker=sym, fields=fields0).rows or []
                        all_rows = [r for r in all_rows if isinstance(r, dict)]
                        strikes_cache_by_symbol[sym] = all_rows
                        exp_dates = _infer_live_expiries_from_strikes(all_rows)
                    except Exception as e:
                        exp_warn.append(f"Live strikes fallback error for {sym}: {type(e).__name__}: {e}")
                        exp_dates = []

                exp_dates_by_symbol[sym] = exp_dates

            def _pick_symbol_and_expiry(*, mode: str) -> Tuple[Optional[str], Optional[str]]:
                for sym in symbols:
                    ds = exp_dates_by_symbol.get(sym) or []
                    if mode == "weekly":
                        ex = _pick_weekly_close_expiry_date(ds, today=now, now_dt=now_dt_utc)
                    else:
                        ex = _pick_nearest_expiry_date(ds, today=now)
                    if ex:
                        return sym, ex
                return None, None

            weekly_sym, weekly_expiry = _pick_symbol_and_expiry(mode="weekly")
            daily_sym, daily_expiry = _pick_symbol_and_expiry(mode="daily")

            def _build_view(*, symbol: Optional[str], expiry: Optional[str], label: str) -> Dict[str, Any]:
                base = {
                    "enabled": False,
                    "label": label,
                    "symbolUsed": symbol,
                    "expiry": str(expiry)[:10] if expiry else None,
                    "spot": None,
                    "bandPct": 0.05,
                    "atmIvPct": None,
                    "greeksAgg": None,
                    "dealerGamma": None,
                    "oiClusters": None,
                    "gammaFlipStrike": None,
                    "addons": None,
                    "warnings": [],
                    "notes": [],
                }
                if not symbol or not expiry:
                    base["notes"] = ["No suitable expiry found for this view."]
                    return base

                fields = ",".join(
                    [
                        "ticker",
                        "tradeDate",
                        "expirDate",
                        "strike",
                        "spotPrice",
                        "stockPrice",
                        "gamma",
                        "theta",
                        "vega",
                        "callOpenInterest",
                        "putOpenInterest",
                        "callVolume",
                        "putVolume",
                        "callMidIv",
                        "putMidIv",
                    ]
                )
                used_chain_sym, chain_rows, chain_warn = _live_chain_with_fallback(
                    client,
                    tickers=[symbol],
                    expiry=expiry,
                    fields=fields,
                )
                # If strikes-by-expiry is empty, fall back to filtering full strikes payload (if we have it).
                if (not chain_rows) and symbol in strikes_cache_by_symbol:
                    chain_rows = _filter_chain_by_expiry(strikes_cache_by_symbol.get(symbol) or [], expiry=expiry)
                    if chain_rows:
                        chain_warn.append("Live strikes-by-expiry empty; used full strikes filtered by expiry.")

                if not chain_rows:
                    base["warnings"] = chain_warn
                    base["notes"] = ["Live strikes returned no usable chain rows for the selected expiry (check entitlement, symbol, or expiry selection)."]
                    return base

                dg = compute_dealer_gamma_context(chain_rows, expiry=expiry, contract_multiplier=100, band_pct=0.05, top_n=5)
                oi = compute_open_interest_clusters(chain_rows, expiry=expiry, band_pct=0.05, top_n=5, cluster_steps=2)

                # Simple greek aggregates near spot band (same band as dealer gamma)
                spot = dg.get("spot")
                lo = float(spot) * (1.0 - 0.05) if spot else None
                hi = float(spot) * (1.0 + 0.05) if spot else None
                w_mode = str(dg.get("weightingMode") or "oi")
                g_sum = 0.0
                t_sum = 0.0
                v_sum = 0.0
                iv_atm = None
                if spot and lo and hi:
                    best_dist = None
                    for r in chain_rows:
                        strike = _to_float(r.get("strike"))
                        if strike is None or not (lo <= float(strike) <= hi):
                            continue
                        gamma = _to_float(r.get("gamma")) or 0.0
                        theta = _to_float(r.get("theta")) or 0.0
                        vega = _to_float(r.get("vega")) or 0.0
                        if w_mode == "oi":
                            w = (_to_float(r.get("callOpenInterest")) or 0.0) + (_to_float(r.get("putOpenInterest")) or 0.0)
                        elif w_mode == "volume":
                            w = (_to_float(r.get("callVolume")) or 0.0) + (_to_float(r.get("putVolume")) or 0.0)
                        else:
                            w = 1.0
                        w = max(0.0, float(w))
                        g_sum += float(gamma) * w * 100.0
                        t_sum += float(theta) * w * 100.0
                        v_sum += float(vega) * w * 100.0

                        dist = abs(float(strike) - float(spot))
                        if best_dist is None or dist < best_dist:
                            best_dist = dist
                            # Prefer call mid iv, fallback to put mid iv
                            iv = _iv_to_pct(r.get("callMidIv")) or _iv_to_pct(r.get("putMidIv"))
                            iv_atm = iv

                # Gamma flip (best-effort, weighted by OI/volume mode)
                gamma_flip = None
                try:
                    if spot is not None:
                        gamma_flip = _compute_gamma_flip_strike(
                            chain_rows,
                            spot=float(spot),
                            band_pct=0.05,
                            weighting_mode=str(w_mode),
                            contract_multiplier=100,
                        )
                except Exception:
                    gamma_flip = None

                # Addon metrics (weekly + nearest cards)
                put_wall = oi.get("putWall") if isinstance(oi, dict) else None
                call_wall = oi.get("callWall") if isinstance(oi, dict) else None
                put_strike = None
                call_strike = None
                try:
                    if isinstance(put_wall, dict):
                        put_strike = _to_float(put_wall.get("peakStrike") or put_wall.get("centerStrike") or put_wall.get("maxStrike"))
                    if isinstance(call_wall, dict):
                        call_strike = _to_float(call_wall.get("peakStrike") or call_wall.get("centerStrike") or call_wall.get("maxStrike"))
                except Exception:
                    put_strike = None
                    call_strike = None

                addons = {
                    "hedgingPressure": compute_hedging_pressure(
                        chain_rows,
                        spot=_to_float(spot),
                        band_pct=0.05,
                        contract_multiplier=100,
                        adv_shares_20d=adv20_shares,
                        weighting_mode=str(w_mode),
                    ),
                    "tailIgnition": compute_tail_ignition(
                        chain_rows,
                        spot=_to_float(spot),
                        put_wall_strike=put_strike,
                        call_wall_strike=call_strike,
                        gamma_flip_strike=gamma_flip,
                        weighting_mode=str(w_mode),
                        contract_multiplier=100,
                    ),
                }

                base.update(
                    {
                        "enabled": True,
                        "symbolUsed": used_chain_sym or symbol,
                        "expiry": str(expiry)[:10],
                        "spot": dg.get("spot"),
                        "atmIvPct": None if iv_atm is None else round(float(iv_atm), 2),
                        "greeksAgg": {
                            "gamma": round(float(g_sum), 3),
                            "theta": round(float(t_sum), 3),
                            "vega": round(float(v_sum), 3),
                            "weightingMode": w_mode,
                        },
                        "dealerGamma": dg,
                        "oiClusters": oi,
                        "gammaFlipStrike": (None if gamma_flip is None else round(float(gamma_flip), 2)),
                        "addons": addons,
                        "warnings": chain_warn,
                        "notes": [
                            "Live, informational only. Dealer gamma context does not change breach odds or any historical stats.",
                            "spotPrice is preferred; stockPrice may be parity-derived intraday.",
                        ],
                    }
                )
                return base

            weekly_view = _build_view(symbol=weekly_sym, expiry=weekly_expiry, label="weeklyFriday")
            daily_view = _build_view(symbol=daily_sym, expiry=daily_expiry, label="nearestDaily")

            # Back-compat: expose a primary view at top-level (weekly preferred).
            primary_view = weekly_view if weekly_view.get("enabled") else daily_view
            any_enabled = bool(weekly_view.get("enabled") or daily_view.get("enabled"))
            live_context = {
                "enabled": any_enabled,
                "symbolUsed": primary_view.get("symbolUsed"),
                "expiry": primary_view.get("expiry"),
                "spot": primary_view.get("spot"),
                "bandPct": 0.05,
                "atmIvPct": primary_view.get("atmIvPct"),
                "greeksAgg": primary_view.get("greeksAgg"),
                "dealerGamma": primary_view.get("dealerGamma"),
                "oiClusters": primary_view.get("oiClusters"),
                "weeklyFriday": weekly_view,
                "nearestDaily": daily_view,
                "volPressure": None,
                "warnings": [*exp_warn, *(primary_view.get("warnings") or [])],
                "notes": [
                    "Live, informational only. Backtest/odds use ORATS EOD and are not affected by these live panels.",
                    "Weekly view targets the Friday weekly expiry (rolls after 4:15pm ET on Fridays).",
                    "Nearest view targets 0DTE/nearest expiry (intraday microstructure).",
                ],
            }
            if not any_enabled:
                live_context["enabled"] = False
                live_context["notes"] = [
                    "Live context unavailable (no usable chain rows for weekly or nearest expiry).",
                ]
                live_context["warnings"] = exp_warn
        else:
            live_context["notes"] = ["Live endpoints not configured on this ORATS client (missing live_* methods)."]
    except Exception:
        # Never fail Engine 2 on live context
        live_context = {
            "enabled": False,
            "symbolUsed": None,
            "expiry": None,
            "spot": None,
            "bandPct": 0.05,
            "atmIvPct": None,
            "greeksAgg": None,
            "dealerGamma": None,
            "oiClusters": None,
            "weeklyFriday": None,
            "nearestDaily": None,
            "volPressure": None,
            "warnings": [],
            "notes": ["Live context unavailable (unexpected error)."],
        }

    # Underlying-level vol supply/demand (same regardless of weekly/nearest)
    try:
        # Use the last available bar date as the volatility as-of date.
        asof_trade = str(bars[-1].trade_date)[:10] if bars else str(now)[:10]
        live_context["volPressure"] = compute_vol_pressure(
            asof=asof_trade,
            dates_sorted=[str(d)[:10] for d in trade_dates],
            iv7_by_date=iv7_by_date,
            iv30_by_date=iv30_by_date,
            rv10_by_date=rv10_by_date,
            slope_by_date=slope_by_date,
            window=60,
        )
    except Exception:
        live_context["volPressure"] = {"enabled": False, "reason": "error"}

    # "Like now" conditional odds: filter historical weeks to the current buckets (regime/macro/season).
    # This is the core desk question: "in conditions like now, how often do 1.0/1.5/2.0× EM breach?"
    like_rows = [r for r in week_rows if str(r.get("regimeBucket")) == regime_bucket_now and str(r.get("macroBucket")) == macro_bucket_now and str(r.get("seasonBucket")) == season_bucket_now]
    per_w: Dict[float, Dict[str, Any]] = {float(w): {"w": float(w), "n": 0, "breachEither": 0, "breachPut": 0, "breachCall": 0, "avgAbsRetPct": 0.0} for w in widths_use}
    for r in like_rows:
        try:
            ret = float(r.get("retPct"))
            em1 = float(r.get("em1sigmaPct"))
        except Exception:
            continue
        abs_ret = abs(ret)
        for w in widths_use:
            dist = float(w) * float(em1)
            breach_put = ret < -dist
            breach_call = ret > dist
            breach = bool(breach_put or breach_call)
            acc = per_w[float(w)]
            acc["n"] += 1
            acc["breachEither"] += 1 if breach else 0
            acc["breachPut"] += 1 if breach_put else 0
            acc["breachCall"] += 1 if breach_call else 0
            acc["avgAbsRetPct"] += float(abs_ret)

    odds_like_now: List[Dict[str, Any]] = []
    for w, acc in per_w.items():
        n = int(acc["n"])
        if n > 0:
            avg_abs = float(acc["avgAbsRetPct"]) / n
            out = dict(acc)
            out["avgAbsRetPct"] = round(avg_abs, 3)
            out["breachEitherPct"] = round(acc["breachEither"] / n * 100.0, 2)
            out["breachPutPct"] = round(acc["breachPut"] / n * 100.0, 2)
            out["breachCallPct"] = round(acc["breachCall"] / n * 100.0, 2)
            odds_like_now.append(out)
        else:
            odds_like_now.append({**acc, "breachEitherPct": None, "breachPutPct": None, "breachCallPct": None})
    odds_like_now.sort(key=lambda x: x["w"])

    # Build aggregated cells output
    cells_out: List[Dict[str, Any]] = []
    for (entry_day_k, reg_k, macro_k, season_k, em_k, wp_k), v in agg.items():
        n = int(v["n"])
        k_b = int(v["breach"])
        k_o = int(v["outside"])
        mae_list = list(v["maePts"] or [])
        loss_list = list(v["lossPts"] or [])
        pb = beta_binomial_mean(k=k_b, n=n, alpha=1.0, beta=1.0)
        po = beta_binomial_mean(k=k_o, n=n, alpha=1.0, beta=1.0)
        mae95 = pctile(mae_list, 95.0)
        loss95 = pctile(loss_list, 95.0)
        mean_loss = (sum(loss_list) / len(loss_list)) if loss_list else None
        cells_out.append(
            {
                "entryDay": entry_day_k,
                "regimeBucket": reg_k,
                "macroBucket": macro_k,
                "seasonBucket": season_k,
                "emMult": float(em_k),
                "wingWidthPts": int(wp_k),
                "n": n,
                "pBreachPct": None if pb is None else round(100.0 * float(pb), 3),
                "pOutsideWingsPct": None if po is None else round(100.0 * float(po), 3),
                "mae95Pts": None if mae95 is None else round(float(mae95), 3),
                "mae95xWing": None if (mae95 is None or wp_k <= 0) else round(float(mae95) / float(wp_k), 3),
                "loss95Pts": None if loss95 is None else round(float(loss95), 3),
                "loss95xWing": None if (loss95 is None or wp_k <= 0) else round(float(loss95) / float(wp_k), 3),
                "meanLossPts": None if mean_loss is None else round(float(mean_loss), 4),
            }
        )

    # Recommendation search for current buckets, prefer emMult=1.0
    policy = {
        # Let caller-supplied risk_target_breach_pct override the default breach cap.
        "maxBreachPct": float(risk_target_breach_pct) if risk_target_breach_pct is not None else float(flags.ENGINE2_POLICY_MAX_BREACH_PCT),
        "maxOutsideWingsPct": float(flags.ENGINE2_POLICY_MAX_OUTSIDE_WINGS_PCT),
        "maxMae95xWing": float(flags.ENGINE2_POLICY_MAX_MAE95_X_WING),
    }
    # Candidate selection: exact bucket first, then graceful fallbacks (so UI isn't empty).
    def _select_candidates(*, macro_bucket: Optional[str], season_bucket: Optional[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for c in cells_out:
            if c.get("entryDay") != ed:
                continue
            if c.get("regimeBucket") != regime_bucket_now:
                continue
            if macro_bucket is not None and c.get("macroBucket") != macro_bucket:
                continue
            if season_bucket is not None and c.get("seasonBucket") != season_bucket:
                continue
            out.append(c)
        return out

    match_used = {
        "entryDay": ed,
        "regimeBucket": regime_bucket_now,
        "macroBucket": macro_bucket_now,
        "seasonBucket": season_bucket_now,
        "fallbackUsed": False,
        "fallbackReason": None,
    }
    candidates = _select_candidates(macro_bucket=macro_bucket_now, season_bucket=season_bucket_now)
    if not candidates:
        # 1) If seasonality is enabled, relax season bucket (keep macro).
        if season_mode != "none":
            c2 = _select_candidates(macro_bucket=macro_bucket_now, season_bucket=None)
            if c2:
                candidates = c2
                match_used.update({"fallbackUsed": True, "fallbackReason": "season_bucket_relaxed"})
        # 2) If macro bucket is MACRO, relax to NORMAL (keep season if possible).
        if (not candidates) and macro_bucket_now == "MACRO":
            c3 = _select_candidates(macro_bucket="NORMAL", season_bucket=(season_bucket_now if season_mode != "none" else None))
            if c3:
                candidates = c3
                match_used.update({"fallbackUsed": True, "fallbackReason": "macro_bucket_relaxed_to_normal", "macroBucket": "NORMAL"})
        # 3) If still empty, relax both macro + season.
        if not candidates:
            c4 = _select_candidates(macro_bucket=None, season_bucket=None)
            if c4:
                candidates = c4
                match_used.update({"fallbackUsed": True, "fallbackReason": "macro_and_season_relaxed"})
    # --- EM preference (regime-adaptive short-strike placement) ---
    _vp = live_context.get("volPressure") or {}
    _vp_state = str(_vp.get("state") or "NEUTRAL").upper() if isinstance(_vp, dict) else "NEUTRAL"
    _dg = live_context.get("dealerGamma") or {}
    _dg_sign = str(_dg.get("netGammaSign") or "unknown").lower() if isinstance(_dg, dict) else "unknown"
    _news_max_adj = 0.0
    try:
        from backend.engine2_advisor import compute_news_gate_score, _load_todays_dms, _extract_dms_context
        _dms_raw = _load_todays_dms()
        if _dms_raw:
            _dms_ctx = _extract_dms_context(_dms_raw)
            _news_max_adj = float((_dms_ctx.get("newsGate") or {}).get("maxAdjustedIntensity", 0))
    except Exception:
        pass

    em_preference = _compute_em_preference(
        regime_score=_regime_score_value(regime_now),
        macro_multiplier=float(macro_now.get("multiplier", 1.0)),
        news_gate_max_adj=_news_max_adj,
        vol_pressure_state=_vp_state,
        dealer_gamma_sign=_dg_sign,
    )
    em_pref = float(em_preference["emPreference"])

    # Desk consensus: shared deterministic risk pre-score for LLM convergence
    _news_gate_for_consensus: Dict[str, Any] = {}
    try:
        if _dms_raw:
            _news_gate_for_consensus = (_dms_ctx or {}).get("newsGate") or {}
    except Exception:
        pass
    try:
        from backend.engine2_advisor import compute_desk_consensus
        desk_consensus = compute_desk_consensus(
            regime_score=_regime_score_value(regime_now),
            regime_bucket=str(regime_now.get("bucket", "MODERATE")),
            macro_multiplier=float(macro_now.get("multiplier", 1.0)),
            news_gate=_news_gate_for_consensus,
            dealer_gamma_sign=_dg_sign,
            vol_pressure_state=_vp_state,
            em_breach_summary=None,  # populated after width comparison
        )
    except Exception:
        desk_consensus = {"riskLevel": "moderate", "suggestedEmFloor": 1.5, "flags": []}

    def _meets(c: Dict[str, Any]) -> bool:
        if c.get("pBreachPct") is None or c.get("pOutsideWingsPct") is None or c.get("mae95xWing") is None:
            return False
        return (
            float(c["pBreachPct"]) <= policy["maxBreachPct"]
            and float(c["pOutsideWingsPct"]) <= policy["maxOutsideWingsPct"]
            and float(c["mae95xWing"]) <= policy["maxMae95xWing"]
        )

    pick = None
    # Pass 1: preferred EM from composite scorer, smallest qualifying wing
    same_em = [c for c in candidates if abs(float(c["emMult"]) - em_pref) < 1e-9]
    for c in sorted(same_em, key=lambda x: int(x["wingWidthPts"])):
        if _meets(c):
            pick = c
            break
    # Pass 2: adjacent EM values in proximity order
    if pick is None:
        for fallback_em in _em_fallback_order(em_pref, em_mults):
            fb_cells = [c for c in candidates if abs(float(c["emMult"]) - fallback_em) < 1e-9]
            for c in sorted(fb_cells, key=lambda x: int(x["wingWidthPts"])):
                if _meets(c):
                    pick = c
                    break
            if pick:
                break
    # Pass 3: any qualifying cell, prefer wider EM (defensive) then smallest wing
    if pick is None:
        ok = [c for c in candidates if _meets(c)]
        ok.sort(key=lambda x: (-float(x["emMult"]), int(x["wingWidthPts"])))
        pick = ok[0] if ok else None

    # If still none, provide best-effort (lowest breach/outside/mae) so UI has a suggestion.
    best_effort = None
    if pick is None and candidates:
        scored = []
        for c in candidates:
            pb = float(c.get("pBreachPct") or 9999.0)
            po = float(c.get("pOutsideWingsPct") or 9999.0)
            m = float(c.get("mae95xWing") or 9999.0)
            scored.append((pb, po, m, int(c.get("wingWidthPts") or 9999), float(c.get("emMult") or 9999.0), c))
        scored.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]))
        best_effort = scored[0][-1] if scored else None
    rec = {
        "entryDay": ed,
        "regimeBucket": regime_bucket_now,
        "macroBucket": macro_bucket_now,
        "seasonBucket": season_bucket_now,
        "seasonalityMode": season_mode,
        "matchUsed": match_used,
        "emPreference": em_preference,
        "policy": policy,
        "recommended": None,
        "bestEffort": None,
        "notes": [],
    }
    if pick is not None:
        rec["recommended"] = {"emMult": pick["emMult"], "wingWidthPts": pick["wingWidthPts"], "n": pick["n"], "pBreachPct": pick["pBreachPct"], "pOutsideWingsPct": pick["pOutsideWingsPct"], "mae95Pts": pick["mae95Pts"], "mae95xWing": pick["mae95xWing"]}
        rec["notes"].append("Meets policy constraints in the matched bucket.")
    else:
        rec["notes"].append("No configuration met constraints for the matched bucket.")
        if best_effort is not None:
            rec["bestEffort"] = {
                "emMult": best_effort["emMult"],
                "wingWidthPts": best_effort["wingWidthPts"],
                "n": best_effort["n"],
                "pBreachPct": best_effort["pBreachPct"],
                "pOutsideWingsPct": best_effort["pOutsideWingsPct"],
                "mae95Pts": best_effort["mae95Pts"],
                "mae95xWing": best_effort["mae95xWing"],
            }
            rec["notes"].append("Showing best-effort (lowest breach/outside/MAE) for transparency.")
        rec["notes"].append("Consider widening wings, reducing size, or relaxing constraints (risk-only engine does not price credit).")

    # Empirical macro vs non-macro effects (risk-only), using a fixed baseline geometry for comparison:
    # EM=1.0 and wing=15pts (if available), otherwise closest.
    baseline_em = 1.0
    baseline_wing = 15
    if wing_pts:
        baseline_wing = min(wing_pts, key=lambda x: abs(int(x) - 15))
    # Choose the closest EM in the configured grid
    if em_mults:
        baseline_em = min(em_mults, key=lambda x: abs(float(x) - 1.0))
    baseline_cells = [c for c in cells_out if c["entryDay"] == ed and abs(float(c["emMult"]) - float(baseline_em)) < 1e-9 and int(c["wingWidthPts"]) == int(baseline_wing)]

    def _split_macro(cells: List[Dict[str, Any]]) -> Dict[str, Any]:
        mac = [x for x in cells if x.get("macroBucket") == "MACRO"]
        nor = [x for x in cells if x.get("macroBucket") == "NORMAL"]
        def _avg(key: str, xs: List[Dict[str, Any]]) -> Optional[float]:
            vals = [float(r[key]) for r in xs if r.get(key) is not None]
            if not vals:
                return None
            return sum(vals) / len(vals)
        return {
            "macro": {"nCells": len(mac), "avgPBreachPct": _avg("pBreachPct", mac), "avgMae95xWing": _avg("mae95xWing", mac)},
            "normal": {"nCells": len(nor), "avgPBreachPct": _avg("pBreachPct", nor), "avgMae95xWing": _avg("mae95xWing", nor)},
        }

    macro_effects = {
        "baseline": {"emMult": float(baseline_em), "wingWidthPts": int(baseline_wing)},
        "overall": _split_macro(baseline_cells),
        "byRegimeBucket": {},
        "notes": ["Macro effect uses smoothed grid probabilities for baseline geometry (risk-only)."],
    }
    for rb in ("LOW", "MODERATE", "ELEVATED", "NO_TRADE"):
        macro_effects["byRegimeBucket"][rb] = _split_macro([c for c in baseline_cells if c.get("regimeBucket") == rb])

    # Backtest summary (fast): derive the "byWidth" table from the already-computed week_rows.
    # This avoids calling backtest_weekly_ic_risk(), which performs many per-day ORATS requests.
    per_width: Dict[float, Dict[str, Any]] = {float(w): {"w": float(w), "n": 0, "breachEither": 0, "breachPut": 0, "breachCall": 0, "avgAbsRetPct": 0.0} for w in widths_use}
    per_quarter: Dict[str, Dict[float, Dict[str, Any]]] = {q: {float(w): {"n": 0, "breachEither": 0} for w in widths_use} for q in ("Q1", "Q2", "Q3", "Q4")}
    for r in week_rows:
        try:
            ret = float(r.get("retPct"))
            em1 = float(r.get("em1sigmaPct"))
            entry_dt = _parse_date(str(r.get("entryDate") or ""))
        except Exception:
            continue
        abs_ret = abs(ret)
        qk = _quarter_key(entry_dt)
        for w in widths_use:
            dist = float(w) * float(em1)
            breach_put = ret < -dist
            breach_call = ret > dist
            breach = bool(breach_put or breach_call)
            acc = per_width[float(w)]
            acc["n"] += 1
            acc["breachEither"] += 1 if breach else 0
            acc["breachPut"] += 1 if breach_put else 0
            acc["breachCall"] += 1 if breach_call else 0
            acc["avgAbsRetPct"] += float(abs_ret)
            qacc = per_quarter[qk][float(w)]
            qacc["n"] += 1
            qacc["breachEither"] += 1 if breach else 0

    by_width: List[Dict[str, Any]] = []
    for w, acc in per_width.items():
        n = int(acc["n"])
        if n > 0:
            avg_abs = float(acc["avgAbsRetPct"]) / n
            out = dict(acc)
            out["avgAbsRetPct"] = round(avg_abs, 3)
            out["breachEitherPct"] = round(acc["breachEither"] / n * 100.0, 2)
            out["breachPutPct"] = round(acc["breachPut"] / n * 100.0, 2)
            out["breachCallPct"] = round(acc["breachCall"] / n * 100.0, 2)
            by_width.append(out)
        else:
            by_width.append({**acc, "breachEitherPct": None, "breachPutPct": None, "breachCallPct": None})
    by_width.sort(key=lambda x: x["w"])

    by_q: Dict[str, Any] = {}
    for qk, wmap in per_quarter.items():
        by_q[qk] = {}
        for w, acc in wmap.items():
            n = int(acc["n"])
            by_q[qk][str(w)] = {"n": n, "breachEitherPct": (round(acc["breachEither"] / n * 100.0, 2) if n else None)}

    bt = {"rowsUsed": int(len(week_rows)), "rows": [], "byWidth": by_width, "byQuarter": by_q, "notes": ["Derived from Engine 2 weekly rows (fast path)."]}
    rec_simple = recommend_width(by_width=by_width, risk_target_breach_pct=float(risk_target_breach_pct))

    # --- Width comparison: 2D EM x Wing matrix ---
    # EM multiplier = primary decision (sets short strikes, determines breach %).
    # Wing width = secondary decision (sets capital at risk, ROC, outside-wings %).
    # Breach is constant across wing widths for a given EM (short strike doesn't move).
    width_comparison: List[Dict[str, Any]] = []
    em_breach_summary: Dict[str, Any] = {}

    def _find_wc_cells(em_val: float, wp_val: int) -> List[Dict[str, Any]]:
        """Filter cells_out for (em, wp) with cascading bucket relaxation."""
        for macro_b, season_b in [
            (macro_bucket_now, season_bucket_now if season_mode != "none" else None),
            (macro_bucket_now, None),
            (None, None),
        ]:
            filt = [
                c for c in cells_out
                if abs(float(c.get("emMult", 0)) - em_val) < 1e-9
                and int(c.get("wingWidthPts", 0)) == wp_val
                and c.get("entryDay") == ed
                and c.get("regimeBucket") == regime_bucket_now
                and (macro_b is None or c.get("macroBucket") == macro_b)
                and (season_b is None or c.get("seasonBucket") == season_b)
            ]
            if filt:
                return filt
        return [
            c for c in cells_out
            if abs(float(c.get("emMult", 0)) - em_val) < 1e-9
            and int(c.get("wingWidthPts", 0)) == wp_val
            and c.get("entryDay") == ed
        ]

    if len(wing_pts) > 1:
        for em in em_mults:
            # Breach % comes from any wing at this EM (it's identical)
            any_wp = wing_pts[0]
            breach_cells = _find_wc_cells(em, int(any_wp))
            if breach_cells:
                _b_vals = [float(c["pBreachPct"]) for c in breach_cells if c.get("pBreachPct") is not None]
                _b_ns = [int(c.get("n", 0)) for c in breach_cells if c.get("pBreachPct") is not None]
                _b_total = sum(_b_ns)
                em_breach = round(sum(b * n for b, n in zip(_b_vals, _b_ns)) / _b_total, 2) if _b_total > 0 else None
            else:
                em_breach = None
            em_breach_summary[str(em)] = em_breach
            survival = round(100.0 - em_breach, 2) if em_breach is not None else None

            for wp in wing_pts:
                wp_cells = _find_wc_cells(em, int(wp))

                # Outside wings % (full loss) -- varies by wing width
                _o_vals = [float(c["pOutsideWingsPct"]) for c in wp_cells if c.get("pOutsideWingsPct") is not None]
                _o_ns = [int(c.get("n", 0)) for c in wp_cells if c.get("pOutsideWingsPct") is not None]
                _o_total = sum(_o_ns)
                outside_pct = round(sum(o * n for o, n in zip(_o_vals, _o_ns)) / _o_total, 2) if _o_total > 0 else None

                mae_vals = [float(c["mae95xWing"]) for c in wp_cells if c.get("mae95xWing") is not None]
                avg_mae95x = round(sum(mae_vals) / len(mae_vals), 3) if mae_vals else None
                loss_vals = [float(c["loss95Pts"]) for c in wp_cells if c.get("loss95Pts") is not None]
                avg_loss95 = round(sum(loss_vals) / len(loss_vals), 2) if loss_vals else None

                # Expected-loss credit proxy: E[loss] = P(partial) * wing/2 + P(outside) * wing
                # Then scale by VRP (volatility risk premium) for realistic premium estimate.
                _ml_vals = [float(c["meanLossPts"]) for c in wp_cells if c.get("meanLossPts") is not None]
                _ml_ns = [int(c.get("n", 0)) for c in wp_cells if c.get("meanLossPts") is not None]
                _ml_total = sum(_ml_ns)
                avg_mean_loss = (sum(m * n for m, n in zip(_ml_vals, _ml_ns)) / _ml_total) if _ml_total > 0 else None

                max_loss = float(wp) * 100.0
                if avg_mean_loss is not None and max_loss > 0:
                    vrp_factor = 1.25 + 0.10 * float(em)
                    credit_proxy = round(avg_mean_loss * 100.0 * vrp_factor, 2)
                    credit_proxy = max(credit_proxy, round(max_loss * 0.02, 2))
                else:
                    credit_proxy = round(max_loss * 0.10 * math.exp(-0.3 * float(em)), 2) if wp else 0.0
                roc = round(credit_proxy / (max_loss - credit_proxy) * 100.0, 2) if (max_loss > credit_proxy > 0) else None
                risk_adj_roc = round(roc * survival / 100.0, 2) if (roc is not None and survival is not None) else None
                total_obs = sum(int(c.get("n", 0)) for c in wp_cells)
                width_comparison.append({
                    "emMult": float(em),
                    "wingWidthPts": int(wp),
                    "breachPct": em_breach,
                    "outsidePct": outside_pct,
                    "fullLossPct": outside_pct,
                    "survivalPct": survival,
                    "creditProxy": credit_proxy,
                    "expectedLoss": round(avg_mean_loss * 100.0, 2) if avg_mean_loss is not None else None,
                    "maxLoss": max_loss,
                    "rocPct": roc,
                    "riskAdjRocPct": risk_adj_roc,
                    "avgMae95xWing": avg_mae95x,
                    "avgLoss95Pts": avg_loss95,
                    "gridCells": len(wp_cells),
                    "totalObs": total_obs,
                })
        width_comparison.sort(key=lambda x: (float(x.get("emMult", 0)), -(x.get("riskAdjRocPct") or 0)))
        for i, wc in enumerate(width_comparison):
            wc["rank"] = i + 1
            if wc["wingWidthPts"] <= 5:
                wc["label"] = "Tight / Higher ROC"
            elif wc["wingWidthPts"] <= 10:
                wc["label"] = "Standard"
            elif wc["wingWidthPts"] <= 15:
                wc["label"] = "Moderate"
            else:
                wc["label"] = "Wide / Safer"

    # Re-run desk consensus with breach data now available
    if em_breach_summary:
        try:
            from backend.engine2_advisor import compute_desk_consensus as _dc
            desk_consensus = _dc(
                regime_score=_regime_score_value(regime_now),
                regime_bucket=str(regime_now.get("bucket", "MODERATE")),
                macro_multiplier=float(macro_now.get("multiplier", 1.0)),
                news_gate=_news_gate_for_consensus,
                dealer_gamma_sign=_dg_sign,
                vol_pressure_state=_vp_state,
                em_breach_summary=em_breach_summary,
            )
        except Exception:
            pass

    # --- Edge Analysis (deterministic 0-100 score) ---
    # SPX analogue of E1's vrpAnalysis: a composite of regime, macro,
    # vol pressure, dealer gamma, news gate and breach % at the preferred
    # EM. Drives the pre-LLM scorecard and feeds the advisor prompt.
    try:
        from backend.spx_ic.edge_score import compute_edge_score
        edge_analysis = compute_edge_score(
            regime_score=_regime_score_value(regime_now),
            regime_bucket=str(regime_now.get("bucket", "MODERATE")),
            macro_multiplier=float(macro_now.get("multiplier", 1.0)),
            vol_pressure_state=_vp_state,
            dealer_gamma_sign=_dg_sign,
            news_gate=_news_gate_for_consensus,
            em_breach_summary=em_breach_summary or None,
            preferred_em=em_pref,
        )
    except Exception as _e:  # pragma: no cover — defensive
        LOG.warning("edge_score failed: %s", _e)
        edge_analysis = None

    # --- Technicals (daily indicators + live overlay; additive, does not affect backtest) ---
    tech_bars: List[TechDailyBar] = []
    for b in bars:
        # only keep fully ordered series, tolerate missing volume/vwap
        if not b or not b.trade_date:
            continue
        tech_bars.append(
            TechDailyBar(
                trade_date=str(b.trade_date)[:10],
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                vwap=b.vwap,
            )
        )
    closes_tech = [float(b.close) for b in tech_bars if b.close is not None and float(b.close) > 0]
    ema = compute_ema_levels(closes_tech, spans=[8, 21, 50, 100, 200]) if closes_tech else {}
    ok_ohlc = bool(tech_bars) and (tech_bars[-1].high is not None) and (tech_bars[-1].low is not None) and (tech_bars[-1].close is not None)

    # Close-based indicators (daily)
    rsi: Dict[str, Any] = {"enabled": False, "period": 14, "value": None, "slope1d": None, "state": None, "notes": []}
    macd: Dict[str, Any] = {
        "enabled": False,
        "fast": 12,
        "slow": 26,
        "signal": 9,
        "macd": None,
        "signalLine": None,
        "hist": None,
        "cross": None,
        "histTrend": None,
        "notes": [],
    }
    boll: Dict[str, Any] = {
        "enabled": False,
        "period": 20,
        "stdev": 2.0,
        "mid": None,
        "upper": None,
        "lower": None,
        "bandwidthPct": None,
        "percentB": None,
        "state": None,
        "squeeze": None,
        "notes": [],
    }
    ema_slopes: Dict[str, Optional[float]] = {}
    try:
        for span in (21, 50, 200):
            if len(closes_tech) >= int(span) + 6:
                ser = _ema_series(closes_tech, int(span))
                if len(ser) >= 6:
                    ema_slopes[f"ema{int(span)}_slope5"] = float(ser[-1]) - float(ser[-6])
            else:
                ema_slopes[f"ema{int(span)}_slope5"] = None
    except Exception:
        ema_slopes = {}

    if len(closes_tech) >= 16:
        rsi_series = compute_rsi_series(closes_tech, period=14)
        rv = rsi_series[-1]
        rp = rsi_series[-2] if len(rsi_series) >= 2 else None
        if rv is not None and math.isfinite(float(rv)):
            slope = None
            if rp is not None and math.isfinite(float(rp)):
                slope = float(rv) - float(rp)
            state = "overbought" if float(rv) >= 70.0 else "oversold" if float(rv) <= 30.0 else "neutral"
            rsi = {
                "enabled": True,
                "period": 14,
                "value": float(rv),
                "slope1d": None if slope is None else float(slope),
                "state": state,
                "notes": ["RSI computed on daily closes (Wilder smoothing)."],
            }

    if len(closes_tech) >= 40:
        m = compute_macd_series(closes_tech, fast=12, slow=26, signal=9)
        macd_series = m.get("macd") or []
        sig_series = m.get("signal") or []
        hist_series = m.get("hist") or []
        mv = macd_series[-1] if macd_series else None
        sv = sig_series[-1] if sig_series else None
        hv = hist_series[-1] if hist_series else None
        cross = None
        hist_trend = None
        if len(macd_series) >= 2 and len(sig_series) >= 2:
            mp = macd_series[-2]
            sp = sig_series[-2]
            if all(x is not None for x in (mp, sp, mv, sv)):
                prev = float(mp) - float(sp)
                cur = float(mv) - float(sv)
                if prev <= 0 and cur > 0:
                    cross = "bullish"
                elif prev >= 0 and cur < 0:
                    cross = "bearish"
        if len(hist_series) >= 2 and hist_series[-2] is not None and hv is not None:
            try:
                hist_trend = "increasing" if float(hv) > float(hist_series[-2]) else "decreasing" if float(hv) < float(hist_series[-2]) else "flat"
            except Exception:
                hist_trend = None
        if mv is not None and sv is not None:
            macd = {
                "enabled": True,
                "fast": 12,
                "slow": 26,
                "signal": 9,
                "macd": float(mv) if mv is not None else None,
                "signalLine": float(sv) if sv is not None else None,
                "hist": float(hv) if hv is not None else None,
                "cross": cross,
                "histTrend": hist_trend,
                "notes": ["MACD computed on daily closes (12/26 EMA, 9 EMA signal)."],
            }

    if len(closes_tech) >= 40:
        bb = compute_bollinger_series(closes_tech, period=20, stdev=2.0)
        mid_s = bb.get("mid") or []
        up_s = bb.get("upper") or []
        lo_s = bb.get("lower") or []
        bw_s = bb.get("bandwidthPct") or []
        pb_s = bb.get("percentB") or []
        mid_v = mid_s[-1] if mid_s else None
        up_v = up_s[-1] if up_s else None
        lo_v = lo_s[-1] if lo_s else None
        bw_v = bw_s[-1] if bw_s else None
        pb_v = pb_s[-1] if pb_s else None
        state = None
        if up_v is not None and lo_v is not None:
            c0 = float(closes_tech[-1])
            if c0 > float(up_v):
                state = "above_upper"
            elif c0 < float(lo_v):
                state = "below_lower"
            else:
                state = "inside"
        squeeze = None
        bw_vals = [float(x) for x in bw_s[-120:] if x is not None and math.isfinite(float(x))]
        if bw_v is not None and bw_vals:
            # simple percentile: bottom 20% => squeeze
            pr = None
            try:
                c = sum(1 for v in bw_vals if v <= float(bw_v))
                pr = c / float(len(bw_vals))
            except Exception:
                pr = None
            if pr is not None:
                squeeze = bool(float(pr) <= 0.20)
        if mid_v is not None and up_v is not None and lo_v is not None:
            boll = {
                "enabled": True,
                "period": 20,
                "stdev": 2.0,
                "mid": float(mid_v),
                "upper": float(up_v),
                "lower": float(lo_v),
                "bandwidthPct": None if bw_v is None else float(bw_v),
                "percentB": None if pb_v is None else float(pb_v),
                "state": state,
                "squeeze": squeeze,
                "notes": ["Bollinger Bands computed on daily closes (20 SMA, 2σ)."],
            }

    # OHLC-based
    ich = compute_ichimoku_levels(tech_bars) if ok_ohlc else {"enabled": False, "notes": ["Insufficient OHLC for Ichimoku."]}
    vwap_proxy = compute_vwap_proxy(tech_bars, window=20) if tech_bars else {"enabled": False}
    candles = detect_candlestick_patterns(tech_bars) if ok_ohlc else {"enabled": False, "patterns": [], "notes": ["Insufficient OHLC for candle patterns."]}
    red_dog = detect_red_dog_reversal(tech_bars) if ok_ohlc else {"enabled": False, "bullish": False, "bearish": False, "notes": ["Insufficient OHLC for Red Dog."]}
    elliott = detect_elliott_pivot_structure(tech_bars, threshold_pct=0.04) if closes_tech else {"enabled": False, "structure": "unclear", "notes": ["Insufficient closes for pivots."]}
    live_price_ctx = fetch_live_price_context_optional(client, ticker=str(underlying).upper())
    live_px = _to_float(live_price_ctx.get("price")) if live_price_ctx else None
    if live_px is None:
        try:
            live_px = _to_float((live_context.get("spot") if isinstance(live_context, dict) else None))
        except Exception:
            live_px = None
        if live_px is not None:
            live_price_ctx = {"price": float(live_px), "source": "chain_spot_fallback", "mode": "fallback", "marketOpen": None}
    level_map: Dict[str, Optional[float]] = {}
    level_map.update(ema)
    if isinstance(boll, dict) and boll.get("enabled"):
        try:
            if boll.get("mid") is not None:
                level_map["bbMid"] = float(boll["mid"])
            if boll.get("upper") is not None:
                level_map["bbUpper"] = float(boll["upper"])
            if boll.get("lower") is not None:
                level_map["bbLower"] = float(boll["lower"])
        except Exception:
            pass
    if isinstance(vwap_proxy, dict) and vwap_proxy.get("enabled") and vwap_proxy.get("value") is not None:
        try:
            level_map["vwapProxy"] = float(vwap_proxy["value"])
        except Exception:
            pass
    if isinstance(ich, dict) and ich.get("enabled"):
        if isinstance(ich.get("tenkan"), (int, float)):
            level_map["tenkan"] = float(ich["tenkan"])
        if isinstance(ich.get("kijun"), (int, float)):
            level_map["kijun"] = float(ich["kijun"])
        cn = ich.get("cloudNow") if isinstance(ich.get("cloudNow"), dict) else None
        if cn and isinstance(cn.get("cloudTop"), (int, float)) and isinstance(cn.get("cloudBottom"), (int, float)):
            level_map["cloudTopNow"] = float(cn["cloudTop"])
            level_map["cloudBottomNow"] = float(cn["cloudBottom"])
    distances = compute_distances(live_price=live_px, levels=level_map)
    last_bar = tech_bars[-1] if tech_bars else None
    last_close = None if (last_bar is None or last_bar.close is None) else float(last_bar.close)
    px_for_narr = float(live_px) if (live_px is not None and float(live_px) > 0) else (float(last_close) if last_close is not None else (float(closes_tech[-1]) if closes_tech else 0.0))

    signals = build_ta_signals(
        price=float(px_for_narr),
        ema_levels=ema,
        ema_slopes=ema_slopes,
        rsi=rsi,
        macd=macd,
        boll=boll,
        ich=ich,
        candles=candles,
        red_dog=red_dog,
        elliott=elliott,
        distances=distances,
    )
    narrative = build_ta_narrative(
        ticker=str(underlying).upper(),
        price=float(px_for_narr),
        last_close=float(last_close) if last_close is not None else float(px_for_narr),
        ema_levels=ema,
        ema_slopes=ema_slopes,
        rsi=rsi,
        macd=macd,
        boll=boll,
        ich=ich,
        candles=candles,
        red_dog=red_dog,
        elliott=elliott,
        signals=signals,
    )
    technicals = {
        "enabled": bool(bool(tech_bars)),
        "ticker": str(underlying).upper(),
        "asOfDate": _fmt_date(now),
        "barDateUsed": None if last_bar is None else str(last_bar.trade_date)[:10],
        "lastDailyClose": None if (last_bar is None or last_bar.close is None) else round(float(last_bar.close), 4),
        "livePrice": None if live_px is None else round(float(live_px), 4),
        "ema": {k: (None if v is None else round(float(v), 4)) for k, v in (ema or {}).items()},
        "rsi": {
            **(rsi if isinstance(rsi, dict) else {"enabled": False}),
            "value": (None if not isinstance(rsi, dict) or rsi.get("value") is None else round(float(rsi["value"]), 4)),
            "slope1d": (None if not isinstance(rsi, dict) or rsi.get("slope1d") is None else round(float(rsi["slope1d"]), 4)),
        },
        "macd": (
            {"enabled": False}
            if not isinstance(macd, dict)
            else {
                **macd,
                "macd": (None if macd.get("macd") is None else round(float(macd["macd"]), 6)),
                "signalLine": (None if macd.get("signalLine") is None else round(float(macd["signalLine"]), 6)),
                "hist": (None if macd.get("hist") is None else round(float(macd["hist"]), 6)),
            }
        ),
        "bollinger": (
            {"enabled": False}
            if not isinstance(boll, dict)
            else {
                **boll,
                "mid": (None if boll.get("mid") is None else round(float(boll["mid"]), 4)),
                "upper": (None if boll.get("upper") is None else round(float(boll["upper"]), 4)),
                "lower": (None if boll.get("lower") is None else round(float(boll["lower"]), 4)),
                "bandwidthPct": (None if boll.get("bandwidthPct") is None else round(float(boll["bandwidthPct"]), 4)),
                "percentB": (None if boll.get("percentB") is None else round(float(boll["percentB"]), 4)),
            }
        ),
        "candles": candles,
        "redDog": red_dog,
        "elliott": elliott,
        "ichimoku": ich,
        "vwapProxy": ({"enabled": False} if not isinstance(vwap_proxy, dict) else {**vwap_proxy, "value": (None if vwap_proxy.get("value") is None else round(float(vwap_proxy["value"]), 4))}),
        "distances": distances,
        "signals": signals,
        "narrative": narrative,
        "notes": [
            "Indicators computed on daily bars (EOD).",
            (
                f"Live overlay mode={live_price_ctx.get('mode')} "
                f"source={live_price_ctx.get('source')}."
            ),
        ],
    }

    # --- Actionable VWAP level (surface a single level for each Engine2 run) ---
    vwap_level: Dict[str, Any] = {"enabled": False, "notes": []}
    try:
        vp = technicals.get("vwapProxy") if isinstance(technicals, dict) else None
        if isinstance(vp, dict) and bool(vp.get("enabled")) and vp.get("value") is not None:
            require_orats = bool(getattr(flags, "ENGINE2_REQUIRE_ORATS_DAILY_VWAP", False))
            if require_orats and str(vp.get("mode") or "") != "orats_daily_vwap":
                vwap_level = {
                    "enabled": False,
                    "notes": [
                        "Pinned to ORATS daily VWAP (ENGINE2_REQUIRE_ORATS_DAILY_VWAP=1).",
                        "ORATS daily VWAP not available for this run; no proxy fallback used.",
                    ],
                }
            else:
                vwap_val = float(vp.get("value"))
                if math.isfinite(vwap_val) and vwap_val > 0:
                    vwap_level = {
                        "enabled": True,
                        "value": round(vwap_val, 4),
                        "mode": str(vp.get("mode") or ""),
                        "window": (None if vp.get("window") is None else int(vp.get("window"))),
                        "barDateUsed": technicals.get("barDateUsed"),
                        "livePrice": technicals.get("livePrice"),
                        "distance": None,
                        "notes": (vp.get("notes") if isinstance(vp.get("notes"), list) else []),
                    }
                    dist = technicals.get("distances") if isinstance(technicals, dict) else None
                    lv = (dist.get("levels") if isinstance(dist, dict) else None) or {}
                    vwap_dist = lv.get("vwapProxy") if isinstance(lv, dict) else None
                    if isinstance(vwap_dist, dict):
                        dp = vwap_dist.get("diffPts")
                        dpc = vwap_dist.get("diffPct")
                        side = None
                        try:
                            dp0 = float(dp)
                            if math.isfinite(dp0):
                                side = "above" if dp0 > 0 else "below" if dp0 < 0 else "at"
                        except Exception:
                            side = None
                        vwap_level["distance"] = {
                            "diffPts": dp,
                            "diffPct": dpc,
                            "side": side,
                        }
    except Exception:
        vwap_level = {"enabled": False, "notes": ["VWAP level unavailable."]}

    # --- Expected Move (weekly Friday options only - excludes dailies) ---
    expected_move: Dict[str, Any] = {"enabled": False, "notes": ["Expected move unavailable."]}
    strike_targets: Optional[Dict[str, Any]] = None
    try:
        # Determine symbols to try based on underlying
        em_symbols: Tuple[str, ...]
        if underlying == "SPX":
            em_symbols = ("SPXW", "SPX", "SPY")
        elif underlying == "QQQ":
            em_symbols = ("QQQ",)
        else:
            em_symbols = (underlying,)
        
        em_result = compute_expected_move_weekly(
            client,
            ticker=underlying,
            today=now,
            symbols=em_symbols,
        )
        
        straddle_em_pct = _to_float(em_result.get("expectedMovePct"))
        orats_em_pct = _to_float(em_result.get("oratsExpectedMovePct"))
        has_em = (straddle_em_pct is not None and straddle_em_pct > 0) or (orats_em_pct is not None and orats_em_pct > 0)

        if has_em:
            expected_move = {
                "enabled": True,
                **em_result,
            }

            # Strike targets follow ORATS EM (delayed -> EOD). Fallback to straddle EM if ORATS EM is unavailable.
            em_pct_for_targets = orats_em_pct if (orats_em_pct is not None and orats_em_pct > 0) else straddle_em_pct
            spot_for_targets = _to_float(em_result.get("smartSpotPrice")) or _to_float(em_result.get("spotPrice"))
            if em_pct_for_targets is not None and spot_for_targets is not None and float(spot_for_targets) > 0:
                strike_targets = compute_strike_targets(
                    expected_move_pct=float(em_pct_for_targets),
                    spot_price=float(spot_for_targets),
                )
                strike_targets["emSource"] = str(
                    em_result.get("oratsExpectedMoveSource")
                    if (orats_em_pct is not None and orats_em_pct > 0)
                    else "straddle"
                )
        else:
            expected_move = {
                "enabled": False,
                **em_result,
            }
        mark("compute.expected_move")
    except Exception as e:
        expected_move = {
            "enabled": False,
            "notes": [f"Expected move computation failed: {type(e).__name__}"],
        }

    telemetry["counts"]["backtest.rowsUsed"] = int(len(week_rows))
    mark("compute.total")
    LOG.info(
        "Engine2 compute done in %.2fs: trade_dates=%s windows=%s week_rows=%s cores_rows=%s",
        (time.perf_counter() - t0),
        int(telemetry["counts"].get("trade_dates", 0)),
        int(telemetry["counts"].get("windows", 0)),
        int(len(week_rows)),
        int(telemetry["counts"].get("orats.cores_rows", 0)),
    )

    # v2: overlay MI v2 regime probabilities so the Command Deck + the
    # tracker share a single source of truth. Best effort; graceful
    # fallback when MI v2 is disabled or not yet calibrated. Mirrors the
    # earnings_logic pattern at backend/earnings_logic.py:1735-1753.
    regime_mi_v2: Optional[Dict[str, Any]] = None
    try:
        if bool(getattr(flags, "ENABLE_MI_V2", False)):
            from backend.market_intel import regime_snapshot as _mi_regime_snapshot
            _mi = _mi_regime_snapshot()
            if _mi is not None:
                _label = str(getattr(_mi, "label", "") or "") or None
                _probs = getattr(_mi, "probabilities", None) or {}
                regime_mi_v2 = {
                    "label":         _label,
                    "probabilities": dict(_probs) if isinstance(_probs, dict) else {},
                    "vol_state":     getattr(_mi, "vol_state", None),
                    "source":        getattr(_mi, "source", "v2_hmm"),
                }
    except Exception as _mi_err:
        LOG.debug("engine2: MI v2 overlay skipped: %s", _mi_err)

    # v2: expose the weekly pool for Monte Carlo consumers. Each row
    # carries the signed close-to-close return + the bucket tags the
    # MC conditioning uses. daily_returns populated when we have per-day
    # bars on the hold window (used by the bootstrap path sampler).
    weeks_out: List[Dict[str, Any]] = []
    for r in week_rows:
        ed_h  = r.get("entryDate") or r.get("entry_date") or r.get("weekStart")
        xp_h  = r.get("expiryDate") or r.get("expiry_date") or r.get("weekEnd")
        epx   = r.get("entryPx") or r.get("entry_close")
        xpx   = r.get("expiryPx") or r.get("expPx") or r.get("expiry_close") or r.get("exitPx")
        # Source of truth in the engine payload is `retPct` (signed % close-
        # to-close return).  Fall back to deriving it from entry/expiry
        # prices if retPct is missing.
        sm = r.get("retPct")
        if sm is None:
            sm = r.get("signedMovePct") or r.get("signed_move_pct") or r.get("returnPct")
        if sm is None and epx and xpx and float(epx) > 0:
            try:
                sm = (float(xpx) - float(epx)) / float(epx) * 100.0
            except Exception:
                sm = None
        row = {
            "entryDate":      ed_h,
            "expiryDate":     xp_h,
            "entryPx":        epx,
            "expiryPx":       xpx,
            "signedMovePct":  (None if sm is None else round(float(sm), 4)),
            "regimeBucket":   r.get("regimeBucket") or r.get("bucket"),
            "macroBucket":    r.get("macroBucket") or r.get("mb"),
            "season":         r.get("seasonBucket") or r.get("seasonality") or r.get("season"),
            "em1sigmaPct":    r.get("em1sigmaPct"),
            "dte":            r.get("dte"),
        }
        # Use mc_simulator-friendly keys so run_weekly_mc can bootstrap
        # without a translation layer.
        row["regime_bucket"] = row["regimeBucket"]
        row["macro_bucket"] = row["macroBucket"]
        row["signed_move_pct"] = row["signedMovePct"]
        weeks_out.append(row)

    out: Dict[str, Any] = {
        "enabled": bool(flags.ENABLE_ENGINE2_SPX_IC),
        "asOfDate": _fmt_date(now),
        "params": {
            "entryDay": ed,
            "years": yrs,
            "widths": [float(x) for x in widths_use],
            "emMults": [float(x) for x in em_mults],
            "wingWidthPts": [int(x) for x in wing_pts],
            "seasonalityMode": season_mode,
            "deskLocked": True,
            "multiWing": bool(flags.ENGINE2_MULTI_WING),
        },
        "underlying": {"symbol": underlying, "isProxy": bool(is_proxy), "notes": proxy_notes},
        # Preserve the engine's existing `current` keys (regime, macro, vwap)
        # while stashing the MI v2 HMM reading under `current.regimeMiV2`.
        "current": {
            "regime": regime_now,
            "macro": macro_now,
            "vwap": vwap_level,
            "regimeMiV2": regime_mi_v2,
        },
        # `regime` top-level mirrors `current.regime` + MI v2 for any
        # consumer that treats regime as a first-class field (parity with
        # /api/breach's `regime.mi_v2` shape).
        "regime": {
            **(regime_now or {}),
            "mi_v2": regime_mi_v2,
        },
        "liveContext": live_context,
        "expectedMove": expected_move,
        "strikeTargets": strike_targets,
        "oddsLikeNow": {
            "regimeBucket": regime_bucket_now,
            "macroBucket": macro_bucket_now,
            "seasonBucket": season_bucket_now,
            "weeksUsed": int(len(like_rows)),
            "byWidth": odds_like_now,
            "notes": ["Conditioned on current buckets (regime/macro/season). Risk-only: breach is expiry-close outside ±(width×EM)."],
        },
        "backtest": bt,
        "recommendation": rec,
        "recSimple": rec_simple,
        "riskGrid": {"cells": cells_out, "count": len(cells_out)},
        "macroEffects": macro_effects,
        "widthComparison": width_comparison,
        "emPreference": em_preference,
        "emBreachSummary": em_breach_summary,
        "deskConsensus": desk_consensus,
        "edgeAnalysis": edge_analysis,
        "technicals": technicals,
        "telemetry": telemetry,
        "notes": proxy_notes,
        # v2: historical weekly pool used by the Wing Console + MC.
        "weeks": weeks_out,
    }
    out["historyBreakerRisk"] = compute_e2_history_breaker_risk(out)

    # v2: strip legacy desk-consensus verdict fields from the primary
    # /api/spx-ic response when E2_EMIT_DESK_CONSENSUS is off. The advisor
    # endpoint still re-computes them internally for the LLM prompt.
    # `emPreference` is preserved regardless — it's a lean-toward ranking
    # (not a TRADE/PASS verdict) and is useful even in Command Deck mode.
    if bool(getattr(flags, "ENABLE_E2_V2", False)) and not bool(
        getattr(flags, "E2_EMIT_DESK_CONSENSUS", False)
    ):
        out.pop("deskConsensus", None)
        out.pop("recSimple", None)

    return out
