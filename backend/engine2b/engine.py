"""Engine 2b — Flex-Expiry SPX Iron Condor payload generator.

Mirrors :func:`backend.spx_ic.engine.compute_engine2_spx_ic` for the core
risk stack (breach %, outside-wing %, MAE, EM, strike targets, regime,
macro, desk consensus, recommendation) but anchored on a user-supplied
``(entry_date, expiry_date)`` pair rather than the same-week Friday.

The Friday-locked engine module is imported read-only — its helpers
(:func:`_compute_em_preference`, :func:`_em_fallback_order`,
:func:`_regime_score_value`) are reused verbatim. The grid loop is
ported with two changes:

1. Windows come from :func:`engine2b.flex_windows.build_flex_windows`
   instead of ``build_weekly_windows_from_trade_dates``.
2. The macro anchor per window is ``entry_date .. expiry_date`` instead
   of ``Mon..Fri of entry week`` — because flex trades don't have to
   live inside one calendar week.

Payload mirrors the keys the existing ``/spx`` renderers expect plus a
``flexExpiry`` block carrying the trade shape and a ``spansHoliday`` UI
hint.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import statistics
import time
from typing import Any, Dict, List, Optional, Tuple

from backend.benzinga_client import BenzingaClient
from backend.config import FeatureFlags
from backend.expected_move import compute_strike_targets
from backend.orats_client import OratsClient, OratsError

from backend.engine2b.flex_analytics import build_flex_analytics
from backend.engine2b.flex_em import compute_expected_move_flex
from backend.engine2b.flex_live_chain import (
    compute_flex_live_chain_targets,
    find_hedge_strike_mid,
)
from backend.engine2b.flex_windows import (
    FlexWindow,
    build_flex_windows,
    classify_holiday,
    derive_target_shape,
)
from backend.engine2b.hedge_sizer import (
    HedgeStrike,
    ShortPosition,
    compute_hedge_sizing,
)
from backend.engine2b.weekend_stress import compute_weekend_stress

from backend.spx_ic.backtest import beta_binomial_mean, pctile, recommend_width
from backend.spx_ic.engine import (
    _compute_em_preference,
    _em_fallback_order,
    _regime_score_value,
)
from backend.spx_ic.ohlc import (
    DailyOHLC,
    fetch_dailies_ohlc_range,
    fetch_hist_cores_range,
    iv_to_em1sigma_pct,
)
from backend.spx_ic.regime import (
    _is_opex_week,
    _is_summer,
    _log_returns,
    _macro_context,
    _parkinson_vol,
    _prefetch_benzinga_economics,
    compute_regime_score_for_date,
    compute_sector_dispersion_series,
)
from backend.spx_ic.utils import (
    _fmt_date,
    _iv_to_pct,
    _parse_date,
    _pct_ret,
    _quarter_key,
    _to_float,
)

LOG = logging.getLogger("engine2b")


_HOLIDAY_LABELS: Dict[Tuple[int, int], str] = {
    (1, 1): "New Year's Day",
    (1, 15): "Martin Luther King Jr. Day",
    (1, 16): "Martin Luther King Jr. Day",
    (1, 17): "Martin Luther King Jr. Day",
    (1, 18): "Martin Luther King Jr. Day",
    (1, 19): "Martin Luther King Jr. Day",
    (1, 20): "Martin Luther King Jr. Day",
    (1, 21): "Martin Luther King Jr. Day",
    (2, 15): "Presidents' Day",
    (2, 16): "Presidents' Day",
    (2, 17): "Presidents' Day",
    (2, 18): "Presidents' Day",
    (2, 19): "Presidents' Day",
    (2, 20): "Presidents' Day",
    (2, 21): "Presidents' Day",
    (5, 25): "Memorial Day",
    (5, 26): "Memorial Day",
    (5, 27): "Memorial Day",
    (5, 28): "Memorial Day",
    (5, 29): "Memorial Day",
    (5, 30): "Memorial Day",
    (5, 31): "Memorial Day",
    (6, 19): "Juneteenth",
    (6, 20): "Juneteenth",
    (7, 3): "Independence Day (observed)",
    (7, 4): "Independence Day",
    (7, 5): "Independence Day (observed)",
    (9, 1): "Labor Day",
    (9, 2): "Labor Day",
    (9, 3): "Labor Day",
    (9, 4): "Labor Day",
    (9, 5): "Labor Day",
    (9, 6): "Labor Day",
    (9, 7): "Labor Day",
    (11, 22): "Thanksgiving",
    (11, 23): "Thanksgiving",
    (11, 24): "Thanksgiving",
    (11, 25): "Thanksgiving",
    (11, 26): "Thanksgiving",
    (11, 27): "Thanksgiving",
    (11, 28): "Thanksgiving",
    (12, 24): "Christmas (observed)",
    (12, 25): "Christmas",
    (12, 26): "Christmas (observed)",
}


def _holiday_in_span(entry: dt.date, expiry: dt.date) -> Optional[Dict[str, Any]]:
    """Return a label like 'Memorial Day (May 25)' for the first NYSE holiday
    in the (entry, expiry) gap, or None.
    """
    from backend.engine15.trading_calendar import is_trading_day

    d = entry + dt.timedelta(days=1)
    while d < expiry:
        if d.weekday() < 5 and not is_trading_day(d):
            label = _HOLIDAY_LABELS.get((d.month, d.day))
            return {
                "date": d.isoformat(),
                "label": label or "NYSE holiday",
                "weekday": d.weekday(),
            }
        d += dt.timedelta(days=1)
    return None


def _macro_bucket(m: Dict[str, Any]) -> str:
    try:
        mult = float(m.get("multiplier") or 1.0)
    except Exception:
        mult = 1.0
    flags0 = m.get("flags") if isinstance(m.get("flags"), dict) else {}
    hi = any(bool(flags0.get(k)) for k in ("CPI", "FOMC", "NFP"))
    return "MACRO" if (mult >= 1.25 or hi) else "NORMAL"


def compute_engine2b_flex_ic(
    *,
    client: OratsClient,
    benzinga_client: Optional[BenzingaClient],
    flags: FeatureFlags,
    underlying_preference: str = "SPX",
    entry_date: dt.date,
    expiry_date: dt.date,
    years: int = 2,
    widths: Optional[List[float]] = None,
    risk_target_breach_pct: float = 25.0,
    today: Optional[dt.date] = None,
    include_live_chain: bool = True,
) -> Dict[str, Any]:
    """Build the Engine 2b flex-expiry payload.

    Args:
        client: ORATS client (same one Engine 2 uses).
        benzinga_client: Optional Benzinga client for macro overlay.
        flags: Feature flags. ``ENABLE_ENGINE2_SPX_IC`` gates the engine
            output the same way Engine 2 does.
        underlying_preference: SPX | SPY | QQQ. SPX↔SPY proxy fallback
            mirrors the Friday engine.
        entry_date: Live trade entry close (must be a trading day).
        expiry_date: Live trade expiry close (must be after entry_date).
        years: Lookback horizon for historical analogues.
        widths: EM-multiple grid (default ``[1.0, 1.5, 2.0, 2.5]`` — wider
            than the Friday default because flex trades are often farther
            from spot).
        risk_target_breach_pct: Policy breach cap for the recommendation.
        today: Override "today" for testing.
    """
    t0 = time.perf_counter()
    telemetry: Dict[str, Any] = {"timingsMs": {}, "counts": {}, "notes": []}

    def mark(name: str) -> None:
        telemetry["timingsMs"][name] = int(round((time.perf_counter() - t0) * 1000.0))

    now = today or dt.date.today()
    if expiry_date <= entry_date:
        raise ValueError(f"expiry_date ({expiry_date}) must be after entry_date ({entry_date}).")

    # Default EM grid for flex includes 2.5× since the holiday-weekend
    # use case explicitly leans on 2.5× short-strike placement.
    widths_use: List[float] = [1.0, 1.5, 2.0, 2.5]
    if widths:
        try:
            parsed = (
                [float(x) for x in widths]
                if isinstance(widths, (list, tuple))
                else [float(x.strip()) for x in str(widths).split(",") if x.strip()]
            )
            parsed = [w for w in parsed if 0.1 <= w <= 5.0]
            if parsed:
                widths_use = sorted(set(parsed))
        except Exception:
            pass
    em_mults = list(widths_use)
    if getattr(flags, "ENGINE2_MULTI_WING", False):
        _raw_wp = [p.strip() for p in str(flags.ENGINE2_WING_WIDTH_PTS).split(",") if p.strip()]
        wing_pts = sorted({int(float(p)) for p in _raw_wp if float(p) > 0}) or [5]
    else:
        wing_pts = [5]

    # ---- Underlying resolution (mirror Engine 2 SPX↔SPY proxy logic) ----
    proxy_notes: List[str] = []
    pref = str(underlying_preference or "SPX").strip().upper()
    if pref not in ("SPX", "SPY", "QQQ"):
        pref = "SPX"
        proxy_notes.append("Invalid underlying preference; defaulted to SPX.")
    underlying = pref
    is_proxy = False
    probe = fetch_dailies_ohlc_range(client, ticker=underlying, start=now - dt.timedelta(days=7), end=now)
    telemetry["counts"]["orats.probe_rows"] = len(probe or [])
    if not probe and pref in ("SPX", "SPY"):
        alt = "SPY" if pref == "SPX" else "SPX"
        alt_probe = fetch_dailies_ohlc_range(client, ticker=alt, start=now - dt.timedelta(days=7), end=now)
        if alt_probe:
            underlying = alt
            is_proxy = True
            proxy_notes.append(f"{pref} unavailable in ORATS dailies; using {alt} as a proxy for this run.")
            probe = alt_probe
            telemetry["counts"]["orats.probe_rows"] = len(probe or [])
    if not probe:
        raise OratsError(f"{underlying} unavailable in ORATS dailies (no rows returned for probe window).")

    # ---- OHLC history ----
    start_hist = now - dt.timedelta(days=int(years) * 365 + 120)
    bars = fetch_dailies_ohlc_range(client, ticker=underlying, start=start_hist, end=now)
    mark("orats.dailies_range")
    trade_dates = [b.trade_date for b in bars]
    bar_by_date: Dict[str, DailyOHLC] = {b.trade_date: b for b in bars if b and b.trade_date}
    idx_by_date: Dict[str, int] = {b.trade_date: i for i, b in enumerate(bars) if b and b.trade_date}
    closes = [float(b.close) for b in bars if b.close is not None]
    logrets_all = _log_returns(closes)
    telemetry["counts"]["orats.dailies_rows"] = len(bars)
    telemetry["counts"]["trade_dates"] = len(trade_dates)

    # ---- Derive target trade shape + analogue windows ----
    target_shape = derive_target_shape(entry_date=entry_date, expiry_date=expiry_date)
    flex_windows = build_flex_windows(
        trade_dates=trade_dates,
        target_entry_weekday=int(target_shape["entryWeekday"]),
        target_sessions=int(target_shape["dteSessions"]),
        target_calendar_days=int(target_shape["dteCalendarDays"]),
        years=int(years),
        today=now,
    )
    telemetry["counts"]["flexWindows"] = len(flex_windows)
    mark("build.windows")

    holiday_in_live_span = _holiday_in_span(entry_date, expiry_date)

    # ---- Benzinga economics prefetch (anchored to entry→expiry per window) ----
    econ_by_date: Dict[str, List[dict]] = {}
    if benzinga_client is not None and flex_windows:
        try:
            econ_start = min(flex_windows[0].entry_date - dt.timedelta(days=7), now - dt.timedelta(days=30))
            econ_end = max(flex_windows[-1].expiry_date + dt.timedelta(days=7), expiry_date + dt.timedelta(days=7))
            rows = _prefetch_benzinga_economics(
                benzinga_client,
                start=econ_start,
                end=econ_end,
                pagesize=1000,
                max_pages=8,
                importance=3,
                country="US",
            )
            for r in rows:
                d0 = str(r.get("date") or "")[:10]
                if d0:
                    econ_by_date.setdefault(d0, []).append(r)
            telemetry["counts"]["benzinga.econ_rows"] = len(rows)
        except Exception:
            telemetry["notes"].append("Benzinga economics prefetch failed (non-fatal).")
    mark("benzinga.economics_prefetch")

    # ---- ORATS IV series via /hist/cores (DTE-scaled per window) ----
    iv7_by_date: Dict[str, float] = {}
    iv30_by_date: Dict[str, float] = {}
    slope_by_date: Dict[str, float] = {}
    try:
        from_core = (now - dt.timedelta(days=int(years) * 365 + 120))
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
        telemetry["notes"].append("ORATS cores IV range fetch failed; falling back to realized vol where needed.")
    mark("orats.cores_iv_range")

    # ---- Sector dispersion (regime input) ----
    sector_tickers = ["XLF", "XLK", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU"]
    try:
        sector_disp = compute_sector_dispersion_series(client, dates=trade_dates, sector_tickers=sector_tickers)
    except Exception:
        sector_disp = {}
    mark("orats.sector_dispersion")

    # ---- Grid loop: per-window EM, regime, macro, MAE, breach grid ----
    week_rows: List[Dict[str, Any]] = []
    iv_weekly_sample: Dict[str, Dict[str, float]] = {}
    macro_by_entry: Dict[str, Dict[str, Any]] = {}
    agg: Dict[Tuple[str, str, str, float, int], Dict[str, Any]] = {}
    ed_label = _entry_day_label(int(target_shape["entryWeekday"]))

    for win in flex_windows:
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

        # Per-window IV → 1σ EM% over this window's calendar DTE.
        iv7 = iv7_by_date.get(ek)
        iv30 = iv30_by_date.get(ek)
        iv_h = iv7 if iv7 is not None else iv30
        em_source = "IV"
        if iv_h is None or float(iv_h) <= 0:
            # Realized-vol fallback so the engine stays alive on sparse IV history.
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
            iv_weekly_sample[ek] = {
                "iv7": float(iv7) if iv7 is not None else float(iv_h),
                "iv30": float(iv30) if iv30 is not None else float(iv_h),
            }

        # Macro anchor: entry_date → expiry_date (NOT the calendar week).
        macro = None
        if benzinga_client is not None:
            econ_rows_span: List[dict] = []
            d0 = entry
            while d0 <= expiry:
                econ_rows_span.extend(econ_by_date.get(_fmt_date(d0), []))
                d0 += dt.timedelta(days=1)
            macro = _macro_context(
                benzinga_client,
                start=entry,
                end=expiry,
                as_of=entry,
                flags=flags,
                economics_rows=econ_rows_span,
            )
        if macro is None:
            macro = {
                "multiplier": 1.0,
                "flags": {"OPEX": bool(_is_opex_week(expiry))},
                "highImpactUS": {"count": 0, "top": []},
                "notes": ["Benzinga unavailable or disabled."],
            }
        macro_by_entry[ek] = macro

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
        regime_bucket = str(r.get("bucket") or "MODERATE")
        mb = _macro_bucket(macro)

        # MAE/MFE inside the hold window (intra-window high/low vs entry).
        up_mae_pct = 0.0
        down_mae_pct = 0.0
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

        # Open-gap (Fri close -> expiry open) — the actual first
        # opportunity to react when the live trade holds 1 session
        # across a closed weekday. ``open`` is missing for some sparse
        # historical rows; we degrade gracefully.
        gap_open_pct: Optional[float] = None
        gap_open_x_em: Optional[float] = None
        gap_open_breach_flag: bool = False
        exp_open = float(exp_bar.open) if (exp_bar and exp_bar.open is not None and exp_bar.open > 0) else None
        if exp_open is not None:
            gap_open_pct = (exp_open / entry_px - 1.0) * 100.0
            if em1sigma_pct > 1e-9:
                gap_open_x_em = float(gap_open_pct) / float(em1sigma_pct)

        week_rows.append(
            {
                "entryDate": ek,
                "expiryDate": fk,
                "dte": int(win.dte_sessions),
                "dteCalendarDays": int(win.dte_calendar_days),
                "spansHoliday": bool(win.spans_holiday),
                "holidayLabel": win.holiday_label,
                "holidayDate": win.holiday_date.isoformat() if win.holiday_date else None,
                "entryPx": round(entry_px, 2),
                "expiryPx": round(exp_px, 2),
                "expiryOpenPx": round(exp_open, 2) if exp_open is not None else None,
                "retPct": round(float(ret_pct), 3),
                "openGapPct": None if gap_open_pct is None else round(float(gap_open_pct), 3),
                "openGapXEm": None if gap_open_x_em is None else round(float(gap_open_x_em), 3),
                "em1sigmaPct": round(float(em1sigma_pct), 3),
                "emSource": em_source,
                "macroMultiplier": round(float(macro.get("multiplier") or 1.0), 3),
                "regimeScore100": float(r.get("score100") or 50.0),
                "regimeBucket": regime_bucket,
                "macroBucket": mb,
                "maeAbsPts": round(float(mae_abs_pts), 2),
                "maeAbsEm": None if mae_abs_em is None else round(float(mae_abs_em), 3),
            }
        )

        # Grid aggregation
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
                k = (regime_bucket, mb, "ALL", float(em), int(wp))
                cell = agg.get(k)
                if cell is None:
                    cell = {"n": 0, "breach": 0, "outside": 0, "maePts": [], "lossPts": []}
                    agg[k] = cell
                cell["n"] += 1
                cell["breach"] += 1 if breach else 0
                cell["outside"] += 1 if outside else 0
                cell["maePts"].append(float(mae_abs_pts))
                loss_pts = max(0.0, float(diff_pts) - float(short_dist_pts))
                loss_pts = min(float(wp), loss_pts)
                cell["lossPts"].append(float(loss_pts))
    mark("grid.loop")

    # ---- Current-conditions macro + regime (anchored on the LIVE entry→expiry span) ----
    macro_now = None
    if benzinga_client is not None:
        econ_rows_now: List[dict] = []
        d0 = max(now, entry_date)
        while d0 <= expiry_date:
            econ_rows_now.extend(econ_by_date.get(_fmt_date(d0), []))
            d0 += dt.timedelta(days=1)
        # If the live window hasn't started yet (entry_date > now), also
        # include now..entry to capture pre-entry headlines.
        if entry_date > now:
            d0 = now
            while d0 < entry_date:
                econ_rows_now.extend(econ_by_date.get(_fmt_date(d0), []))
                d0 += dt.timedelta(days=1)
        macro_now = _macro_context(
            benzinga_client,
            start=min(entry_date, now),
            end=expiry_date,
            as_of=now,
            flags=flags,
            economics_rows=econ_rows_now,
        )
    if macro_now is None:
        macro_now = {
            "multiplier": 1.0,
            "flags": {"OPEX": bool(_is_opex_week(expiry_date))},
            "highImpactUS": {"count": 0, "top": []},
            "notes": ["Benzinga unavailable or disabled."],
        }
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

    # ---- "Odds like now" — filter windows to current regime/macro bucket ----
    like_rows = [
        r for r in week_rows
        if str(r.get("regimeBucket")) == regime_bucket_now and str(r.get("macroBucket")) == macro_bucket_now
    ]
    per_w: Dict[float, Dict[str, Any]] = {
        float(w): {"w": float(w), "n": 0, "breachEither": 0, "breachPut": 0, "breachCall": 0, "avgAbsRetPct": 0.0}
        for w in widths_use
    }
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
            acc = per_w[float(w)]
            acc["n"] += 1
            acc["breachEither"] += 1 if (breach_put or breach_call) else 0
            acc["breachPut"] += 1 if breach_put else 0
            acc["breachCall"] += 1 if breach_call else 0
            acc["avgAbsRetPct"] += float(abs_ret)
    odds_like_now: List[Dict[str, Any]] = []
    for w, acc in per_w.items():
        n = int(acc["n"])
        if n > 0:
            out = dict(acc)
            out["avgAbsRetPct"] = round(float(acc["avgAbsRetPct"]) / n, 3)
            out["breachEitherPct"] = round(acc["breachEither"] / n * 100.0, 2)
            out["breachPutPct"] = round(acc["breachPut"] / n * 100.0, 2)
            out["breachCallPct"] = round(acc["breachCall"] / n * 100.0, 2)
            odds_like_now.append(out)
        else:
            odds_like_now.append({**acc, "breachEitherPct": None, "breachPutPct": None, "breachCallPct": None})
    odds_like_now.sort(key=lambda x: x["w"])

    # ---- Aggregated grid cells ----
    cells_out: List[Dict[str, Any]] = []
    for (reg_k, macro_k, season_k, em_k, wp_k), v in agg.items():
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
                "entryDay": ed_label,
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

    # ---- Width comparison + breach summary (per-EM) ----
    width_comparison, em_breach_summary = _build_width_comparison(
        cells_out=cells_out,
        em_mults=em_mults,
        wing_pts=wing_pts,
        ed_label=ed_label,
        regime_bucket_now=regime_bucket_now,
        macro_bucket_now=macro_bucket_now,
    )

    # ---- Live EM for the requested expiry ----
    expected_move: Dict[str, Any] = {"enabled": False, "notes": ["Expected move unavailable."]}
    strike_targets: Optional[Dict[str, Any]] = None
    try:
        em_symbols: Tuple[str, ...]
        if underlying == "SPX":
            em_symbols = ("SPXW", "SPX", "SPY")
        elif underlying == "QQQ":
            em_symbols = ("QQQ",)
        else:
            em_symbols = (underlying,)

        em_result = compute_expected_move_flex(
            client,
            ticker=underlying,
            today=now,
            expiry=expiry_date,
            symbols=em_symbols,
        )
        em_pct = _to_float(em_result.get("expectedMovePct"))
        if em_pct is not None and em_pct > 0:
            expected_move = {"enabled": True, **em_result}
            spot_for_targets = _to_float(em_result.get("smartSpotPrice")) or _to_float(em_result.get("spotPrice"))
            if spot_for_targets is not None and float(spot_for_targets) > 0:
                strike_targets = compute_strike_targets(
                    expected_move_pct=float(em_pct),
                    spot_price=float(spot_for_targets),
                )
                strike_targets["emSource"] = "straddle"
        else:
            expected_move = {"enabled": False, **em_result}
        mark("compute.expected_move")
    except Exception as e:
        expected_move = {"enabled": False, "notes": [f"Expected move computation failed: {type(e).__name__}"]}

    # ---- EM preference + recommendation ----
    em_preference = _compute_em_preference(
        regime_score=_regime_score_value(regime_now),
        macro_multiplier=float(macro_now.get("multiplier", 1.0)),
        news_gate_max_adj=0.0,
        vol_pressure_state="NEUTRAL",
        dealer_gamma_sign="unknown",
    )
    em_pref = float(em_preference["emPreference"])

    try:
        from backend.engine2_advisor import compute_desk_consensus
        desk_consensus = compute_desk_consensus(
            regime_score=_regime_score_value(regime_now),
            regime_bucket=str(regime_now.get("bucket", "MODERATE")),
            macro_multiplier=float(macro_now.get("multiplier", 1.0)),
            news_gate={},
            dealer_gamma_sign="unknown",
            vol_pressure_state="NEUTRAL",
            em_breach_summary=em_breach_summary or None,
        )
    except Exception:
        desk_consensus = {"riskLevel": "moderate", "suggestedEmFloor": 1.5, "flags": []}

    # Edge Analysis (deterministic 0-100 SPX edge score). Flex doesn't
    # currently surface live dealer gamma / vol pressure / news gate, so
    # those inputs default to neutral; regime + macro + breach drive the
    # signal for the flex card.
    try:
        from backend.spx_ic.edge_score import compute_edge_score
        edge_analysis = compute_edge_score(
            regime_score=_regime_score_value(regime_now),
            regime_bucket=str(regime_now.get("bucket", "MODERATE")),
            macro_multiplier=float(macro_now.get("multiplier", 1.0)),
            vol_pressure_state="NEUTRAL",
            dealer_gamma_sign="unknown",
            news_gate={},
            em_breach_summary=em_breach_summary or None,
            preferred_em=em_pref,
        )
    except Exception as _e:  # pragma: no cover — defensive
        LOG.warning("edge_score (flex) failed: %s", _e)
        edge_analysis = None

    policy = {
        "maxBreachPct": float(risk_target_breach_pct) if risk_target_breach_pct is not None else float(flags.ENGINE2_POLICY_MAX_BREACH_PCT),
        "maxOutsideWingsPct": float(flags.ENGINE2_POLICY_MAX_OUTSIDE_WINGS_PCT),
        "maxMae95xWing": float(flags.ENGINE2_POLICY_MAX_MAE95_X_WING),
    }
    rec = _build_recommendation(
        cells_out=cells_out,
        em_pref=em_pref,
        em_mults=em_mults,
        ed_label=ed_label,
        regime_bucket_now=regime_bucket_now,
        macro_bucket_now=macro_bucket_now,
        policy=policy,
        em_preference=em_preference,
    )

    # ---- Backtest summary (by width, derived from week_rows) ----
    per_width: Dict[float, Dict[str, Any]] = {
        float(w): {"w": float(w), "n": 0, "breachEither": 0, "breachPut": 0, "breachCall": 0, "avgAbsRetPct": 0.0}
        for w in widths_use
    }
    per_quarter: Dict[str, Dict[float, Dict[str, Any]]] = {
        q: {float(w): {"n": 0, "breachEither": 0} for w in widths_use} for q in ("Q1", "Q2", "Q3", "Q4")
    }
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
            acc = per_width[float(w)]
            acc["n"] += 1
            acc["breachEither"] += 1 if (breach_put or breach_call) else 0
            acc["breachPut"] += 1 if breach_put else 0
            acc["breachCall"] += 1 if breach_call else 0
            acc["avgAbsRetPct"] += float(abs_ret)
            qacc = per_quarter[qk][float(w)]
            qacc["n"] += 1
            qacc["breachEither"] += 1 if (breach_put or breach_call) else 0

    by_width: List[Dict[str, Any]] = []
    for w, acc in per_width.items():
        n = int(acc["n"])
        if n > 0:
            out = dict(acc)
            out["avgAbsRetPct"] = round(float(acc["avgAbsRetPct"]) / n, 3)
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
    bt = {
        "rowsUsed": int(len(week_rows)),
        "rows": [],
        "byWidth": by_width,
        "byQuarter": by_q,
        "notes": ["Derived from Engine 2b flex-expiry rows."],
    }
    rec_simple = recommend_width(by_width=by_width, risk_target_breach_pct=float(risk_target_breach_pct))

    # ---- Flex analytics (holiday-class + open-gap + cohort breach) ----
    target_holiday_label: Optional[str] = None
    if holiday_in_live_span and isinstance(holiday_in_live_span.get("date"), str):
        try:
            target_holiday_label = classify_holiday(dt.date.fromisoformat(holiday_in_live_span["date"][:10]))
        except Exception:
            target_holiday_label = holiday_in_live_span.get("label")
    flex_analytics = build_flex_analytics(
        week_rows=week_rows,
        widths=widths_use,
        regime_bucket_now=regime_bucket_now,
        macro_bucket_now=macro_bucket_now,
        target_holiday_label=target_holiday_label,
        target_spans_holiday=bool(target_shape.get("spansHoliday")),
    )
    mark("flex.analytics")

    # ---- Live chain probe (the actual broker context for the live expiry) ----
    live_chain_block: Dict[str, Any] = {
        "enabled": False,
        "notes": ["Live-chain probe not requested or unavailable."],
    }
    em_pct_for_live = _to_float(expected_move.get("expectedMovePct") if isinstance(expected_move, dict) else None)
    if include_live_chain and em_pct_for_live and em_pct_for_live > 0:
        try:
            if underlying == "SPX":
                lc_symbols = ("SPXW", "SPX", "SPY")
            elif underlying == "QQQ":
                lc_symbols = ("QQQ",)
            else:
                lc_symbols = (underlying,)
            live_chain_block = compute_flex_live_chain_targets(
                client,
                ticker=underlying,
                today=now,
                expiry=expiry_date,
                em_pct=float(em_pct_for_live),
                em_mults=em_mults,
                wing_pts=wing_pts,
                symbols=lc_symbols,
            )
        except Exception as e:
            live_chain_block = {
                "enabled": False,
                "notes": [f"Live-chain probe failed: {type(e).__name__}: {e}"],
            }
    mark("flex.live_chain")

    # ---- Weekend Stress Gauge (holiday-spanning trades only) ----
    weekend_stress_block: Dict[str, Any] = {
        "enabled": False,
        "level": "UNKNOWN",
        "notes": ["Weekend stress gauge runs only for holiday-spanning trades."],
    }
    if include_live_chain and bool(target_shape.get("spansHoliday")):
        try:
            weekend_stress_block = compute_weekend_stress(
                client,
                ticker=underlying,
                today=now,
                near_expiry=expiry_date,
            )
        except Exception as e:
            weekend_stress_block = {
                "enabled": False,
                "level": "UNKNOWN",
                "notes": [f"Weekend stress gauge failed: {type(e).__name__}: {e}"],
            }
    mark("flex.weekend_stress")

    # ---- Hedge Sizer (live single-strike pull + 3-tier reference) ----
    hedge_sizer_block: Dict[str, Any] = {
        "enabled": False,
        "notes": ["Hedge sizer requires include_live_chain=1 and a usable live chain."],
    }
    if (
        include_live_chain
        and isinstance(live_chain_block, dict)
        and live_chain_block.get("enabled")
        and live_chain_block.get("spotPrice")
        and isinstance(live_chain_block.get("targets"), list)
    ):
        # Anchor on the trade the advisor will most often recommend
        # for holiday-weekend trades: 2.0× EM × $5 wing. If that exact
        # row is missing we fall back to the first $5-wing target with
        # a real net credit, then to any $5-wing target.
        targets = live_chain_block["targets"]
        ref_target = None
        for t in targets:
            try:
                if int(t.get("wingWidthPts") or 0) == 5 and abs(float(t.get("emMult") or 0) - 2.0) < 0.01:
                    ref_target = t
                    break
            except Exception:
                continue
        if ref_target is None:
            for t in targets:
                if int(t.get("wingWidthPts") or 0) == 5 and t.get("netMidCredit") not in (None, 0):
                    ref_target = t
                    break
        if ref_target is None and targets:
            ref_target = targets[0]

        if ref_target is None or ref_target.get("netMidCredit") in (None,):
            hedge_sizer_block = {
                "enabled": False,
                "notes": ["No usable live-chain target with a net mid credit; hedge sizer disabled."],
            }
        else:
            try:
                spot_now = float(live_chain_block["spotPrice"])
                credit_per = float(ref_target.get("netMidCredit") or 0.0)
                # Max loss per IC in points = wing - credit (the live chain
                # already returns this as ``maxLossPerContract`` in points).
                max_loss_per = float(ref_target.get("maxLossPerContract") or 0.0)
                short_pos = ShortPosition(
                    contracts=20,  # reference count; UI lets desk scale this live
                    max_loss_per_contract=max_loss_per,
                    credit_per_contract=credit_per,
                    label=(
                        f"{int(ref_target.get('emMult', 2)*1000)/1000.0:g}× EM "
                        f"{ref_target.get('shortPut')}/{ref_target.get('longPut')}P + "
                        f"{ref_target.get('shortCall')}/{ref_target.get('longCall')}C "
                        f"(${ref_target.get('wingWidthPts')} wing)"
                    ),
                )
                # Default hedge strike distance: ±2.5%, snapped to the
                # nearest live strike. ~2.5% is the empirical sweet
                # spot for SPX 4-DTE — cheap (<$0.10 mid) but ITM at
                # the ±3% stress design point.
                call_hedge_info = find_hedge_strike_mid(
                    client,
                    ticker=underlying,
                    today=now,
                    expiry=expiry_date,
                    spot=spot_now,
                    target_distance_pct=2.5,
                    side="call",
                )
                put_hedge_info = find_hedge_strike_mid(
                    client,
                    ticker=underlying,
                    today=now,
                    expiry=expiry_date,
                    spot=spot_now,
                    target_distance_pct=2.5,
                    side="put",
                )
                hedge_call = HedgeStrike(
                    strike=float(call_hedge_info.get("strike") or spot_now * 1.025),
                    side="call",
                    mid_price=call_hedge_info.get("midPrice"),
                    distance_pct=float(call_hedge_info.get("actualDistancePct") or 2.5),
                )
                hedge_put = HedgeStrike(
                    strike=float(put_hedge_info.get("strike") or spot_now * 0.975),
                    side="put",
                    mid_price=put_hedge_info.get("midPrice"),
                    distance_pct=float(put_hedge_info.get("actualDistancePct") or -2.5),
                )
                hedge_sizer_block = compute_hedge_sizing(
                    short_position=short_pos,
                    hedge_call=hedge_call,
                    hedge_put=hedge_put,
                    spot=spot_now,
                    stress_gap_pct=3.0,
                    target_caps_pct=[50.0, 33.0, 20.0],
                    asymmetric={"upCap": 50.0, "downCap": 20.0},
                )
                hedge_sizer_block["referenceTarget"] = {
                    "emMult": ref_target.get("emMult"),
                    "wingWidthPts": ref_target.get("wingWidthPts"),
                    "shortPut": ref_target.get("shortPut"),
                    "longPut": ref_target.get("longPut"),
                    "shortCall": ref_target.get("shortCall"),
                    "longCall": ref_target.get("longCall"),
                }
                hedge_sizer_block["hedgeStrikeLookups"] = {
                    "call": call_hedge_info,
                    "put": put_hedge_info,
                }
            except Exception as e:
                hedge_sizer_block = {
                    "enabled": False,
                    "notes": [f"Hedge sizer failed: {type(e).__name__}: {e}"],
                }
    mark("flex.hedge_sizer")

    mark("compute.total")
    LOG.info(
        "Engine2b flex compute done in %.2fs: windows=%s rows=%s entry=%s exp=%s",
        (time.perf_counter() - t0),
        len(flex_windows),
        len(week_rows),
        entry_date.isoformat(),
        expiry_date.isoformat(),
    )

    out: Dict[str, Any] = {
        "enabled": bool(getattr(flags, "ENABLE_ENGINE2_SPX_IC", False)) and bool(getattr(flags, "ENABLE_E2B_FLEX_EXPIRY", False)),
        "asOfDate": _fmt_date(now),
        "params": {
            "underlying": underlying,
            "entryDate": entry_date.isoformat(),
            "expiryDate": expiry_date.isoformat(),
            "years": int(years),
            "widths": [float(x) for x in widths_use],
            "emMults": [float(x) for x in em_mults],
            "wingWidthPts": [int(x) for x in wing_pts],
            "riskTargetBreachPct": float(risk_target_breach_pct),
        },
        "underlying": {"symbol": underlying, "isProxy": bool(is_proxy), "notes": proxy_notes},
        "flexExpiry": {
            "entryDate": entry_date.isoformat(),
            "expiryDate": expiry_date.isoformat(),
            "entryWeekday": int(target_shape["entryWeekday"]),
            "expiryWeekday": int(target_shape["expiryWeekday"]),
            "dteSessions": int(target_shape["dteSessions"]),
            "dteCalendarDays": int(target_shape["dteCalendarDays"]),
            "spansHoliday": bool(target_shape["spansHoliday"]),
            "holiday": holiday_in_live_span,
            "holidayLabel": target_holiday_label,
            "analoguesFound": int(len(flex_windows)),
            "rowsWithBars": int(len(week_rows)),
            "notes": [
                "Historical analogues match the live trade's entry weekday + sessions + calendar-DTE shape.",
                "Macro / regime / desk consensus reuse Engine 2 helpers (read-only).",
                "Friday-engine main flow at /api/spx-ic is untouched.",
            ],
        },
        "flexAnalytics": flex_analytics,
        "liveChain": live_chain_block,
        "weekendStress": weekend_stress_block,
        "hedgeSizer": hedge_sizer_block,
        "current": {
            "regime": regime_now,
            "macro": macro_now,
        },
        "regime": {**(regime_now or {})},
        "liveContext": {
            "enabled": False,
            "notes": [
                "Live dealer-gamma overlay is not computed for the flex-expiry view in this release.",
                "Use the Friday flow on the same /spx page for dealer-gamma context.",
            ],
        },
        "expectedMove": expected_move,
        "strikeTargets": strike_targets,
        "oddsLikeNow": {
            "regimeBucket": regime_bucket_now,
            "macroBucket": macro_bucket_now,
            "seasonBucket": "ALL",
            "weeksUsed": int(len(like_rows)),
            "byWidth": odds_like_now,
            "notes": [
                "Conditioned on current regime + macro buckets only (season disabled for flex).",
                "Breach is expiry-close outside ±(width × EM) over the flex window's hold span.",
            ],
        },
        "backtest": bt,
        "recommendation": rec,
        "recSimple": rec_simple,
        "riskGrid": {"cells": cells_out, "count": len(cells_out)},
        "widthComparison": width_comparison,
        "emPreference": em_preference,
        "emBreachSummary": em_breach_summary,
        "deskConsensus": desk_consensus,
        "edgeAnalysis": edge_analysis,
        "weeks": week_rows,
        "telemetry": telemetry,
        "notes": proxy_notes,
    }
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry_day_label(weekday: int) -> str:
    return {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}.get(int(weekday), "mon")


def _build_width_comparison(
    *,
    cells_out: List[Dict[str, Any]],
    em_mults: List[float],
    wing_pts: List[int],
    ed_label: str,
    regime_bucket_now: str,
    macro_bucket_now: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build the 2D EM × Wing width-comparison grid + per-EM breach summary.

    Mirrors :func:`backend.spx_ic.engine.compute_engine2_spx_ic` width
    comparison block (~lines 1304-1409) without the seasonality fallback
    (flex doesn't use seasonality buckets).
    """
    width_comparison: List[Dict[str, Any]] = []
    em_breach_summary: Dict[str, Any] = {}

    def _find_cells(em_val: float, wp_val: int) -> List[Dict[str, Any]]:
        for macro_b in (macro_bucket_now, None):
            filt = [
                c for c in cells_out
                if abs(float(c.get("emMult", 0)) - em_val) < 1e-9
                and int(c.get("wingWidthPts", 0)) == wp_val
                and c.get("entryDay") == ed_label
                and c.get("regimeBucket") == regime_bucket_now
                and (macro_b is None or c.get("macroBucket") == macro_b)
            ]
            if filt:
                return filt
        return [
            c for c in cells_out
            if abs(float(c.get("emMult", 0)) - em_val) < 1e-9
            and int(c.get("wingWidthPts", 0)) == wp_val
            and c.get("entryDay") == ed_label
        ]

    if not wing_pts:
        return [], {}

    for em in em_mults:
        any_wp = wing_pts[0]
        breach_cells = _find_cells(em, int(any_wp))
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
            wp_cells = _find_cells(em, int(wp))
            _o_vals = [float(c["pOutsideWingsPct"]) for c in wp_cells if c.get("pOutsideWingsPct") is not None]
            _o_ns = [int(c.get("n", 0)) for c in wp_cells if c.get("pOutsideWingsPct") is not None]
            _o_total = sum(_o_ns)
            outside_pct = round(sum(o * n for o, n in zip(_o_vals, _o_ns)) / _o_total, 2) if _o_total > 0 else None

            mae_vals = [float(c["mae95xWing"]) for c in wp_cells if c.get("mae95xWing") is not None]
            avg_mae95x = round(sum(mae_vals) / len(mae_vals), 3) if mae_vals else None
            loss_vals = [float(c["loss95Pts"]) for c in wp_cells if c.get("loss95Pts") is not None]
            avg_loss95 = round(sum(loss_vals) / len(loss_vals), 2) if loss_vals else None

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
    return width_comparison, em_breach_summary


def _build_recommendation(
    *,
    cells_out: List[Dict[str, Any]],
    em_pref: float,
    em_mults: List[float],
    ed_label: str,
    regime_bucket_now: str,
    macro_bucket_now: str,
    policy: Dict[str, float],
    em_preference: Dict[str, Any],
) -> Dict[str, Any]:
    """Pick a recommended (emMult, wingWidthPts) cell using the same three-pass
    logic as the Friday engine (preferred EM → adjacent EM → any qualifying)."""

    def _meets(c: Dict[str, Any]) -> bool:
        if c.get("pBreachPct") is None or c.get("pOutsideWingsPct") is None or c.get("mae95xWing") is None:
            return False
        return (
            float(c["pBreachPct"]) <= policy["maxBreachPct"]
            and float(c["pOutsideWingsPct"]) <= policy["maxOutsideWingsPct"]
            and float(c["mae95xWing"]) <= policy["maxMae95xWing"]
        )

    def _select(*, macro_bucket: Optional[str]) -> List[Dict[str, Any]]:
        out = []
        for c in cells_out:
            if c.get("entryDay") != ed_label:
                continue
            if c.get("regimeBucket") != regime_bucket_now:
                continue
            if macro_bucket is not None and c.get("macroBucket") != macro_bucket:
                continue
            out.append(c)
        return out

    match_used = {
        "entryDay": ed_label,
        "regimeBucket": regime_bucket_now,
        "macroBucket": macro_bucket_now,
        "seasonBucket": "ALL",
        "fallbackUsed": False,
        "fallbackReason": None,
    }
    candidates = _select(macro_bucket=macro_bucket_now)
    if not candidates:
        c2 = _select(macro_bucket=None)
        if c2:
            candidates = c2
            match_used.update({"fallbackUsed": True, "fallbackReason": "macro_bucket_relaxed"})

    pick = None
    same_em = [c for c in candidates if abs(float(c["emMult"]) - em_pref) < 1e-9]
    for c in sorted(same_em, key=lambda x: int(x["wingWidthPts"])):
        if _meets(c):
            pick = c
            break
    if pick is None:
        for fallback_em in _em_fallback_order(em_pref, em_mults):
            fb_cells = [c for c in candidates if abs(float(c["emMult"]) - fallback_em) < 1e-9]
            for c in sorted(fb_cells, key=lambda x: int(x["wingWidthPts"])):
                if _meets(c):
                    pick = c
                    break
            if pick:
                break
    if pick is None:
        ok = [c for c in candidates if _meets(c)]
        ok.sort(key=lambda x: (-float(x["emMult"]), int(x["wingWidthPts"])))
        pick = ok[0] if ok else None

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
        "entryDay": ed_label,
        "regimeBucket": regime_bucket_now,
        "macroBucket": macro_bucket_now,
        "seasonBucket": "ALL",
        "seasonalityMode": "none",
        "matchUsed": match_used,
        "emPreference": em_preference,
        "policy": policy,
        "recommended": None,
        "bestEffort": None,
        "notes": [],
    }
    if pick is not None:
        rec["recommended"] = {
            "emMult": pick["emMult"],
            "wingWidthPts": pick["wingWidthPts"],
            "n": pick["n"],
            "pBreachPct": pick["pBreachPct"],
            "pOutsideWingsPct": pick["pOutsideWingsPct"],
            "mae95Pts": pick["mae95Pts"],
            "mae95xWing": pick["mae95xWing"],
        }
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
        rec["notes"].append("Consider widening wings, reducing size, or relaxing constraints (risk-only — does not price credit).")
    return rec


__all__ = ["compute_engine2b_flex_ic"]
