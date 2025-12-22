from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.benzinga_client import BenzingaClient
from backend.config import get_flags
from backend.earnings_calendar import benzinga_next_earnings
from backend.event_risk_overlay import compute_event_risk_overlay_optional
from backend.orats_client import OratsClient, OratsError
from backend.regime_overlay import apply_event_risk_adjustment, compute_regime_backtest_view, compute_regime_overlay
from backend.skew_overlay import compute_skew_overlay
from backend.stats_utils import beta_posterior_from_counts
from backend.trade_builder import compute_trade_builder
from backend.wing_recommendation import compute_wing_recommendation
from backend.mc_simulator import bootstrap_tas_stability, optimize_wings_risk_only, run_monte_carlo


LOG = logging.getLogger(__name__)

# Phase 1 (Directional Breach) constants
# Spec: moveDirection is FLAT if abs(signedMovePct) < ~0.01
DIR_FLAT_EPSILON_PCT = 0.01
TAIL_BIAS_RATE_THRESHOLD_PP = 5.0
# "significantly larger" overshoot: use a conservative pp threshold
TAIL_BIAS_OVERSHOOT_THRESHOLD_PP = 10.0


class BreachInputError(ValueError):
    pass


def _parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(str(s)[:10])


def _fmt_date(d: dt.date) -> str:
    return d.isoformat()


def _is_valid_ticker(ticker: str) -> bool:
    # spec: uppercase A-Z, 1–6 chars; keep simple but allow '.' or '-' if later needed
    if not ticker:
        return False
    t = ticker.strip().upper()
    if len(t) < 1 or len(t) > 8:
        return False
    for ch in t:
        if not (ch.isalnum() or ch in ".-"):
            return False
    return True


def classify_timing(annc_tod: Any) -> str:
    """Classify earnings announcement timing as AMC/BMO/UNK using ORATS anncTod."""
    if annc_tod is None:
        return "UNK"
    s = str(annc_tod).strip().upper()
    if not s:
        return "UNK"
    if "AMC" in s or "AFTER" in s:
        return "AMC"
    if "BMO" in s or "BEFORE" in s:
        return "BMO"

    # numeric HHMM heuristic (e.g. 1630)
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) in (3, 4):
        try:
            if len(digits) == 3:
                hh = int(digits[0])
                mm = int(digits[1:])
            else:
                hh = int(digits[:2])
                mm = int(digits[2:])
            minutes = hh * 60 + mm
            if minutes >= (16 * 60):  # 4pm ET-ish
                return "AMC"
            if minutes <= (9 * 60 + 30):  # 9:30am ET-ish
                return "BMO"
        except ValueError:
            return "UNK"
    return "UNK"


@dataclass(frozen=True)
class DailyBar:
    tradeDate: str
    open: Optional[float]
    clsPx: Optional[float]


def _first_row(rows: list[dict]) -> Optional[dict]:
    if not rows:
        return None
    return rows[0]


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def fetch_daily_bar(client: OratsClient, ticker: str, trade_date: str) -> Optional[DailyBar]:
    resp = client.hist_dailies(ticker=ticker, trade_date=trade_date, fields="ticker,tradeDate,clsPx,open")
    row = _first_row(resp.rows)
    if not row:
        return None
    return DailyBar(
        tradeDate=str(row.get("tradeDate") or row.get("trade_date") or trade_date)[:10],
        open=_to_float(row.get("open")),
        clsPx=_to_float(row.get("clsPx") or row.get("close") or row.get("cls_px")),
    )


def find_trading_day(
    get_bar: Callable[[str], Optional[DailyBar]],
    start: dt.date,
    direction: int,
    max_steps: int,
) -> Optional[DailyBar]:
    """Probe for the nearest trading day by stepping day-by-day and calling get_bar(date_str)."""
    cur = start
    for _ in range(max_steps + 1):
        bar = get_bar(_fmt_date(cur))
        if bar and (bar.clsPx is not None or bar.open is not None):
            return bar
        cur = cur + dt.timedelta(days=direction)
    return None


def find_trading_day_with_shift(
    get_bar: Callable[[str], Optional[DailyBar]],
    *,
    start: dt.date,
    direction: int,
    max_steps: int,
    require: Callable[[DailyBar], bool] | None = None,
) -> tuple[Optional[DailyBar], Optional[int]]:
    """
    Like find_trading_day, but also returns a shift metric:
    - shiftDays: calendar-day distance between the first probed date and the returned bar date.

    Note: This is telemetry. It intentionally measures probing distance, not “trading day distance”.
    """
    cur = start
    for _ in range(max_steps + 1):
        bar = get_bar(_fmt_date(cur))
        if bar and (bar.clsPx is not None or bar.open is not None):
            if require is None or require(bar):
                try:
                    used = _parse_date(bar.tradeDate or _fmt_date(cur))
                except Exception:
                    used = cur
                return bar, abs((used - start).days)
        cur = cur + dt.timedelta(days=direction)
    return None, None


def get_prior_trading_day(client: OratsClient, ticker: str, date_: dt.date, max_steps: int = 10) -> Optional[DailyBar]:
    return find_trading_day(lambda d: fetch_daily_bar(client, ticker, d), date_ - dt.timedelta(days=1), -1, max_steps)


def get_next_trading_day(client: OratsClient, ticker: str, date_: dt.date, max_steps: int = 10) -> Optional[DailyBar]:
    return find_trading_day(lambda d: fetch_daily_bar(client, ticker, d), date_ + dt.timedelta(days=1), +1, max_steps)


def _date_shift_days(expected: dt.date, actual_date_str: Optional[str]) -> Optional[int]:
    if not actual_date_str:
        return None
    try:
        actual = _parse_date(actual_date_str)
    except Exception:
        return None
    return abs((actual - expected).days)


def _shift_days(expected: Optional[dt.date], actual_date_str: Optional[str]) -> Optional[int]:
    """
    Calendar-day shift between an expected date and the actual bar/core date used.
    Returns 0 if exact, >0 if substituted, or None if unknown.
    """

    if expected is None or not actual_date_str:
        return None
    try:
        actual = _parse_date(str(actual_date_str)[:10])
    except Exception:
        return None
    return abs((actual - expected).days)


def _imp_to_pct(imp_ern_mv: Any) -> Optional[float]:
    v = _to_float(imp_ern_mv)
    if v is None:
        return None
    v = abs(v)
    # reconcile ORATS conventions:
    # - some feeds deliver 4.5 for 4.5%
    # - some deliver 0.045 for 4.5%
    if v <= 1.0:
        return v * 100.0
    return v


def _pct_move(a: float, b: float) -> float:
    return abs(b - a) / a * 100.0


def _current_snapshot(client: OratsClient, *, ticker: str, as_of_date: str) -> Dict[str, Any]:
    """
    Current-ish snapshot for UI (does not affect model stats).
    Provides a stable source for "assumed price" and "current earnings implied move"
    so the UI doesn't fall back to last earnings close when the chain isn't fetched.
    """
    # Walk back from as_of_date to find the latest available snapshot. This avoids
    # accidentally using "last earnings close" in the UI when chain data isn't fetched.
    out: Dict[str, Any] = {"asOfDate": str(as_of_date)[:10], "stockPrice": None, "impErnMv": None, "impliedMovePct": None, "source": None}
    try:
        start = _parse_date(str(as_of_date)[:10])
    except Exception:
        start = dt.date.today()

    # Try up to 7 prior calendar days to handle weekends/holidays.
    for i in range(0, 8):
        d0 = start - dt.timedelta(days=i)
        ds = _fmt_date(d0)

        # Cores is best (stockPrice + impErnMv).
        try:
            cores = client.hist_cores(ticker=ticker, trade_date=ds, fields="ticker,tradeDate,stockPrice,impErnMv").rows
            row = _first_row(cores) if cores else None
            px = _to_float(row.get("stockPrice")) if row else None
            if row and px is not None:
                out["asOfDate"] = str(row.get("tradeDate") or ds)[:10]
                out["source"] = "cores"
                out["stockPrice"] = _round2(px)
                out["impErnMv"] = row.get("impErnMv")
                out["impliedMovePct"] = _round2(_imp_to_pct(row.get("impErnMv")))
                return out
        except Exception:
            pass

        # Fallback: dailies close for price.
        try:
            bar = fetch_daily_bar(client, ticker, ds)
            if bar and bar.clsPx is not None:
                out["asOfDate"] = str(bar.tradeDate or ds)[:10]
                out["source"] = "dailies"
                out["stockPrice"] = _round2(bar.clsPx)
                return out
        except Exception:
            pass

    return out


def compute_current_snapshot(client: OratsClient, *, ticker: str) -> Dict[str, Any]:
    """Public helper: latest-available price/EM snapshot for UI and trade builder."""
    return _current_snapshot(client, ticker=ticker, as_of_date=_fmt_date(dt.date.today()))


def _mean(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    return sum(xs) / len(xs)


def _round2(v: Optional[float]) -> Optional[float]:
    if v is None:
        return None
    return round(float(v), 2)


def _move_direction(signed_move_pct: Optional[float]) -> Optional[str]:
    """Return UP/DOWN/FLAT for a signed move (percent), else None if missing."""
    if signed_move_pct is None:
        return None
    if abs(float(signed_move_pct)) < DIR_FLAT_EPSILON_PCT:
        return "FLAT"
    return "UP" if signed_move_pct > 0 else "DOWN"


def _tail_bias(
    *,
    up_breach_rate_pct: Optional[float],
    down_breach_rate_pct: Optional[float],
    avg_up_overshoot_pct: Optional[float],
    avg_down_overshoot_pct: Optional[float],
) -> str:
    """Classify tail bias as DOWN/UP/NEUTRAL using spec heuristics."""
    if up_breach_rate_pct is None or down_breach_rate_pct is None:
        return "NEUTRAL"

    diff_pp = float(down_breach_rate_pct) - float(up_breach_rate_pct)
    if diff_pp > TAIL_BIAS_RATE_THRESHOLD_PP:
        return "DOWN"
    if diff_pp < -TAIL_BIAS_RATE_THRESHOLD_PP:
        return "UP"

    # Overshoot tiebreaker if rates are close.
    if avg_up_overshoot_pct is None or avg_down_overshoot_pct is None:
        return "NEUTRAL"
    os_diff_pp = float(avg_down_overshoot_pct) - float(avg_up_overshoot_pct)
    if os_diff_pp > TAIL_BIAS_OVERSHOOT_THRESHOLD_PP:
        return "DOWN"
    if os_diff_pp < -TAIL_BIAS_OVERSHOOT_THRESHOLD_PP:
        return "UP"
    return "NEUTRAL"


def _quarter_key(d: dt.date) -> str:
    q = ((d.month - 1) // 3) + 1
    return f"Q{q}"


def _rate_pct(numer: int, denom: int) -> Optional[float]:
    if denom <= 0:
        return None
    return (numer / denom) * 100.0


def _recommendation(
    *,
    events_used: int,
    breach_rate_k1_pct: Optional[float],
    near_09_pct: Optional[float],
    avg_ratio: Optional[float],
    max_ratio: Optional[float],
    breach_delta_pp: Optional[float],
) -> str:
    # Heuristic labels (spec):
    #   Avoid if events_used < 3
    #   Avoid if breach_rate(k=1.0) >= 40 OR max_ratio_realized_to_implied >= 2.0
    #   Tight if breach_rate <= 10 AND near_breach_rate(0.9) <= 20 AND avg_ratio <= 0.8
    #   Wide if breach_rate >= 25 OR near_breach_rate(0.9) >= 40
    #   else Standard
    if events_used < 3:
        return "Avoid (low sample)"

    br = breach_rate_k1_pct if breach_rate_k1_pct is not None else 100.0
    n09 = near_09_pct if near_09_pct is not None else 100.0
    ar = avg_ratio if avg_ratio is not None else 999.0
    mx = max_ratio if max_ratio is not None else 999.0

    # Base rules
    rec = "Standard"
    if br >= 40.0 or mx >= 2.0:
        rec = "Avoid"
    elif br <= 10.0 and n09 <= 20.0 and ar <= 0.8:
        rec = "Tight"
    elif br >= 25.0 or n09 >= 40.0:
        rec = "Wide"
    else:
        rec = "Standard"

    if rec == "Avoid":
        return rec

    # Seasonality biasing rules (spec):
    # - If breach_delta_pp >= +15 => minimum label is “Wide” (unless Avoid)
    # - If breach_delta_pp <= -10 AND quarter stats otherwise safe => allow “Tight”
    if breach_delta_pp is not None and breach_delta_pp >= 15.0:
        if rec in ("Tight", "Standard"):
            rec = "Wide"
    if breach_delta_pp is not None and breach_delta_pp <= -10.0:
        # only tighten if the quarter itself looks safe by the original "Tight" conditions
        if br <= 10.0 and n09 <= 20.0 and ar <= 0.8:
            rec = "Tight"

    return rec


def _confidence_from_beta_ci(*, n: int, lo: float, hi: float) -> str:
    """
    Map posterior uncertainty to a coarse confidence label.
    Why: prevent small-sample estimates from driving asymmetric sizing.
    """

    if n < 6:
        return "LOW"
    width = float(hi) - float(lo)
    if n >= 20 and width <= 0.20:
        return "HIGH"
    if n >= 12 and width <= 0.30:
        return "MED"
    return "LOW"


def compute_breach_stats(
    client: OratsClient,
    ticker: str,
    n: int = 20,
    years: int = 5,
    k: float = 1.0,
    trade_builder_inputs: Optional[Dict[str, Any]] = None,
    today: Optional[dt.date] = None,
    flags_override: Any = None,
    next_event_override: Optional[Dict[str, Any]] = None,
    benzinga_client: BenzingaClient | None = None,
) -> Dict[str, Any]:
    """
    Compute ORATS earnings implied-move breach stats and overlays.

    Response shape is backwards compatible: existing keys are preserved, and new keys are appended.

    Minimal example (new keys only; many existing keys omitted for brevity):

        {
          "summary": {
            "upBreachRatePct": 25.0,
            "downBreachRatePct": 25.0,
            "avgUpOvershootPct": 50.0,
            "avgDownOvershootPct": 300.0,
            "upBreaches": 1,
            "downBreaches": 1,
            "tailBias": "DOWN"
          },
          "events": [
            {
              "signedMovePct": -8.0,
              "moveDirection": "DOWN",
              "upBreach": false,
              "downBreach": true,
              "breachSide": "DOWN",
              "upOvershootPct": null,
              "downOvershootPct": 300.0
            }
          ],
          "quarters": {
            "Q1": {
              "quarterUpBreachRatePct": 33.33,
              "quarterDownBreachRatePct": 0.0
            }
          },
          "wingRecommendation": {
            "tas": -0.8,
            "structureMode": "AUTO_EQUAL_DELTA",
            "baseWingMultiple": 1.50,
            "putWingMultiple": 2.03,
            "callWingMultiple": 0.98,
            "recommendationLabel": "WIDEN_PUTS_TIGHTEN_CALLS",
            "confidence": "LOW"
          },
          "skewOverlay": {
            "current": {"skewQuality": "MISSING", "notes": "…"},
            "atEvents": {"2025-03-01": {"skewQuality": "MISSING", "notes": "…"}}
          }
        }
    """
    if not _is_valid_ticker(ticker):
        raise BreachInputError("Invalid ticker. Use A-Z/0-9 (optionally '.' or '-') and keep it short.")
    if n <= 0 or n > 50:
        raise BreachInputError("n must be between 1 and 50")
    if years <= 0 or years > 10:
        raise BreachInputError("years must be between 1 and 10")
    if k <= 0:
        raise BreachInputError("k must be > 0")

    t = ticker.strip().upper()
    flags = flags_override if flags_override is not None else get_flags()
    now = today or dt.date.today()

    # Step 1: earnings events
    earn_resp = client.hist_earnings(t)
    events_raw = earn_resp.rows
    # For MC anchoring: track nearest upcoming earnings event (earnDate >= now).
    next_event_raw: Optional[dict] = None
    next_event_date: Optional[dt.date] = None
    parsed: List[Tuple[dt.date, dict]] = []
    for r in events_raw:
        ed = r.get("earnDate") or r.get("earn_date") or r.get("date")
        if not ed:
            continue
        try:
            d = _parse_date(str(ed))
        except ValueError:
            continue
        if d >= now:
            if next_event_date is None or d < next_event_date:
                next_event_date = d
                next_event_raw = r
        parsed.append((d, r))

    parsed.sort(key=lambda x: x[0], reverse=True)
    cutoff = now - dt.timedelta(days=365 * years)
    parsed = [(d, r) for (d, r) in parsed if d >= cutoff]
    parsed = parsed[:n]
    # IMPORTANT: "current quarter" should be based on *today/latest available pricing date*,
    # not the most recent earnings event in the lookback.
    current_quarter_key: Optional[str] = None

    # Step 2-5: per-event computations
    out_events: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    implied_all: List[float] = []
    realized_all: List[float] = []
    breaches: List[bool] = []
    above_breach_all: List[float] = []
    above_breach_vs_k_all: List[float] = []
    realized_if_breach: List[float] = []
    ratios_all: List[float] = []
    # Phase 1 directional aggregates (baseline)
    up_overshoot_all: List[float] = []
    down_overshoot_all: List[float] = []
    up_breaches_all: int = 0
    down_breaches_all: int = 0

    quarter_acc: Dict[str, Dict[str, Any]] = {
        "Q1": {
            "events_total": 0,
            "events_used": 0,
            "breaches": 0,  # at request k
            "breaches_k1": 0,  # at k=1.0 (for recommendation)
            "near_08": 0,
            "near_09": 0,
            "ratios": [],
            "above_breach": [],
            "up_breaches": 0,
            "down_breaches": 0,
            "up_overshoot": [],
            "down_overshoot": [],
            "realized": [],
            "implied": [],
            "max_ratio": None,
        },
        "Q2": {
            "events_total": 0,
            "events_used": 0,
            "breaches": 0,
            "breaches_k1": 0,
            "near_08": 0,
            "near_09": 0,
            "ratios": [],
            "above_breach": [],
            "up_breaches": 0,
            "down_breaches": 0,
            "up_overshoot": [],
            "down_overshoot": [],
            "realized": [],
            "implied": [],
            "max_ratio": None,
        },
        "Q3": {
            "events_total": 0,
            "events_used": 0,
            "breaches": 0,
            "breaches_k1": 0,
            "near_08": 0,
            "near_09": 0,
            "ratios": [],
            "above_breach": [],
            "up_breaches": 0,
            "down_breaches": 0,
            "up_overshoot": [],
            "down_overshoot": [],
            "realized": [],
            "implied": [],
            "max_ratio": None,
        },
        "Q4": {
            "events_total": 0,
            "events_used": 0,
            "breaches": 0,
            "breaches_k1": 0,
            "near_08": 0,
            "near_09": 0,
            "ratios": [],
            "above_breach": [],
            "up_breaches": 0,
            "down_breaches": 0,
            "up_overshoot": [],
            "down_overshoot": [],
            "realized": [],
            "implied": [],
            "max_ratio": None,
        },
    }

    for earn_date, raw in parsed:
        qk = _quarter_key(earn_date)
        quarter_acc[qk]["events_total"] += 1

        annc_tod = raw.get("anncTod") or raw.get("annc_tod") or raw.get("anncTOD")
        timing = classify_timing(annc_tod)

        row_notes: List[str] = []
        pricing_date_used: Optional[str] = None
        pricing_date_shift_days: Optional[int] = None
        realized_window_shift_days: Optional[int] = None
        imp_raw: Any = None
        implied_pct: Optional[float] = None

        close_date_used: Optional[str] = None
        open_date_used: Optional[str] = None
        close_px: Optional[float] = None
        open_px: Optional[float] = None
        realized_pct: Optional[float] = None
        signed_move_pct: Optional[float] = None
        move_direction: Optional[str] = None

        breach: Optional[bool] = None
        above_breach_pct: Optional[float] = None
        above_breach_pct_vs_k: Optional[float] = None
        up_breach: Optional[bool] = None
        down_breach: Optional[bool] = None
        breach_side: Optional[str] = None
        up_overshoot_pct: Optional[float] = None
        down_overshoot_pct: Optional[float] = None

        # Determine pricing date and realized window dates per spec.
        # Strict-mode guardrail: when enabled, probe until we find a bar with the field we need.
        if flags.STRICT_REALIZED_WINDOW:
            prior_bar, _ = find_trading_day_with_shift(
                lambda d: fetch_daily_bar(client, t, d),
                start=earn_date - dt.timedelta(days=1),
                direction=-1,
                max_steps=10,
                require=lambda b: b.clsPx is not None,
            )
            next_bar, _ = find_trading_day_with_shift(
                lambda d: fetch_daily_bar(client, t, d),
                start=earn_date + dt.timedelta(days=1),
                direction=+1,
                max_steps=10,
                require=lambda b: b.open is not None,
            )
        else:
            prior_bar = get_prior_trading_day(client, t, earn_date)
            next_bar = get_next_trading_day(client, t, earn_date)
        earn_bar = fetch_daily_bar(client, t, _fmt_date(earn_date))

        if timing == "AMC":
            pricing_date_used = _fmt_date(earn_date)

            if earn_bar and earn_bar.clsPx is not None:
                close_date_used = earn_bar.tradeDate
                close_px = earn_bar.clsPx
            else:
                row_notes.append("missing dailies close on earnDate")

            if next_bar and next_bar.open is not None:
                open_date_used = next_bar.tradeDate
                open_px = next_bar.open
            else:
                row_notes.append("missing dailies open on next trading day")
            # Telemetry: expected realized window open is earnDate+1 (calendar). If markets are closed, this can shift.
            realized_window_shift_days = _shift_days(earn_date + dt.timedelta(days=1), open_date_used)

        elif timing == "BMO":
            if prior_bar:
                pricing_date_used = prior_bar.tradeDate
            else:
                row_notes.append("missing prior trading day (for BMO pricing date)")

            if prior_bar and prior_bar.clsPx is not None:
                close_date_used = prior_bar.tradeDate
                close_px = prior_bar.clsPx
            else:
                row_notes.append("missing dailies close on prior trading day")

            if earn_bar and earn_bar.open is not None:
                open_date_used = earn_bar.tradeDate
                open_px = earn_bar.open
            else:
                row_notes.append("missing dailies open on earnDate")
            # Telemetry: expected close date is earnDate-1 (calendar). Prior trading day can be shifted by weekends/holidays.
            realized_window_shift_days = _shift_days(earn_date - dt.timedelta(days=1), close_date_used)

        else:
            # Spec: either fallback close(prior)->open(next) OR mark unknown timing and skip breach calc.
            row_notes.append("unknown timing (anncTod); excluded from breach stats")
            if prior_bar and prior_bar.clsPx is not None:
                close_date_used = prior_bar.tradeDate
                close_px = prior_bar.clsPx
            if next_bar and next_bar.open is not None:
                open_date_used = next_bar.tradeDate
                open_px = next_bar.open
            realized_window_shift_days = None

        # Step 3: implied move from cores using pricing_date_used
        if timing in ("AMC", "BMO") and pricing_date_used:
            # if cores missing for date, retry with nearest prior trading day (max 5)
            expected_pricing_date = _parse_date(str(pricing_date_used)[:10])
            cores_used_date = pricing_date_used
            cores_row: Optional[dict] = None
            cores_date = _parse_date(cores_used_date)
            found = False
            for i in range(0, 5):
                try:
                    cores_resp = client.hist_cores(
                        ticker=t,
                        trade_date=_fmt_date(cores_date),
                        fields="ticker,tradeDate,stockPrice,impErnMv",
                    )
                    cores_row = _first_row(cores_resp.rows)
                except OratsError as e:
                    LOG.warning("cores fetch failed %s %s: %s", t, cores_date, e)
                    cores_row = None

                if cores_row and (cores_row.get("impErnMv") is not None):
                    cores_used_date = str(cores_row.get("tradeDate") or _fmt_date(cores_date))[:10]
                    found = True
                    break
                cores_date = cores_date - dt.timedelta(days=1)
            if found:
                pricing_date_used = cores_used_date
                pricing_date_shift_days = _shift_days(expected_pricing_date, cores_used_date)
            else:
                # If we never found impErnMv, preserve None shift (unknown actual).
                pricing_date_shift_days = None

            if not cores_row or cores_row.get("impErnMv") is None:
                row_notes.append("missing cores impErnMv for pricing date after retries")
            else:
                imp_raw = cores_row.get("impErnMv")
                implied_pct = _imp_to_pct(imp_raw)

        # Step 4: realized move
        if close_px is not None and open_px is not None and close_px > 0:
            realized_pct = _pct_move(close_px, open_px)
            signed_move_pct = ((float(open_px) - float(close_px)) / float(close_px)) * 100.0
            move_direction = _move_direction(signed_move_pct)

        # Step 5: breach + above breach (only for valid events with implied+realized and known timing)
        valid_for_stats = timing in ("AMC", "BMO") and (implied_pct is not None) and (realized_pct is not None)
        strict_reject_reason: Optional[str] = None
        if flags.STRICT_REALIZED_WINDOW and timing in ("AMC", "BMO"):
            # Strict realized-window guardrail:
            # reject events where the realized window had to be shifted away from the spec anchor dates
            # (earnDate±1 calendar probing start). This is intentionally conservative and is OFF by default.
            if realized_window_shift_days is not None and realized_window_shift_days > 0:
                valid_for_stats = False
                strict_reject_reason = f"shifted {timing} realized window (strict)"
        if valid_for_stats:
            implied_k = float(implied_pct) * float(k)
            breach = realized_pct > implied_k
            if breach and implied_pct and implied_pct > 0:
                above_breach_pct = (realized_pct - implied_pct) / implied_pct * 100.0
            # Optional additive: overshoot defined vs the actual threshold (k-consistent).
            # Overshoot vs threshold = (absMove - k*implied) / (k*implied)
            if flags.ADD_K_CONSISTENT_OVERSHOOT and breach and implied_k and implied_k > 0:
                above_breach_pct_vs_k = (realized_pct - implied_k) / implied_k * 100.0

            # Directional breach / overshoot (Phase 1)
            if signed_move_pct is not None and implied_k > 0:
                up_breach = float(signed_move_pct) > implied_k
                down_breach = float(signed_move_pct) < -implied_k
                breach_side = "UP" if up_breach else "DOWN" if down_breach else None
                if up_breach:
                    up_overshoot_pct = ((float(signed_move_pct) - implied_k) / implied_k) * 100.0
                if down_breach:
                    down_overshoot_pct = ((abs(float(signed_move_pct)) - implied_k) / implied_k) * 100.0
            else:
                up_breach = False
                down_breach = False
                breach_side = None

            implied_all.append(implied_pct)
            realized_all.append(realized_pct)
            breaches.append(bool(breach))
            if breach:
                realized_if_breach.append(realized_pct)
                if above_breach_pct is not None:
                    above_breach_all.append(above_breach_pct)
                if above_breach_pct_vs_k is not None:
                    above_breach_vs_k_all.append(above_breach_pct_vs_k)

            # Quarter seasonality accumulators
            q = quarter_acc[qk]
            q["events_used"] += 1
            q["implied"].append(implied_pct)
            q["realized"].append(realized_pct)

            ratio = None
            if implied_pct and implied_pct > 0:
                ratio = realized_pct / implied_pct
                q["ratios"].append(ratio)
                ratios_all.append(ratio)
                # float-tolerant comparisons so values like 0.899999999 don't miss 0.9
                eps = 1e-12
                if ratio + eps >= 0.8:
                    q["near_08"] += 1
                if ratio + eps >= 0.9:
                    q["near_09"] += 1
                if q["max_ratio"] is None or ratio > q["max_ratio"]:
                    q["max_ratio"] = ratio

            breach_k1 = realized_pct > implied_pct  # k=1.0
            if breach_k1:
                q["breaches_k1"] += 1
            if breach:
                q["breaches"] += 1
                if above_breach_pct is not None:
                    q["above_breach"].append(above_breach_pct)

            # Directional accumulators (Phase 1)
            if up_breach is True:
                up_breaches_all += 1
                q["up_breaches"] += 1
                if up_overshoot_pct is not None:
                    up_overshoot_all.append(float(up_overshoot_pct))
                    q["up_overshoot"].append(float(up_overshoot_pct))
            if down_breach is True:
                down_breaches_all += 1
                q["down_breaches"] += 1
                if down_overshoot_pct is not None:
                    down_overshoot_all.append(float(down_overshoot_pct))
                    q["down_overshoot"].append(float(down_overshoot_pct))
        else:
            # record skip reason
            if timing == "UNK":
                reason = "unknown timing"
            elif strict_reject_reason:
                reason = strict_reject_reason
            else:
                reason = "missing implied/realized data"
            skipped.append({"earnDate": _fmt_date(earn_date), "reason": reason})

        out_events.append(
            {
                "earnDate": _fmt_date(earn_date),
                "anncTod": None if annc_tod is None else str(annc_tod),
                "timing": timing,
                "pricingDateUsed": pricing_date_used,
                # Data quality telemetry (additive): quantify when results rely on substituted dates.
                "pricingDateShiftDays": pricing_date_shift_days if (flags.ADD_EVENT_SHIFT_TELEMETRY) else None,
                "realizedWindowShiftDays": realized_window_shift_days if (flags.ADD_EVENT_SHIFT_TELEMETRY) else None,
                "impErnMv": imp_raw,
                "impliedMovePct": _round2(implied_pct),
                "closeDateUsed": close_date_used,
                "closePx": _round2(close_px),
                "openDateUsed": open_date_used,
                "openPx": _round2(open_px),
                "realizedMovePct": _round2(realized_pct),
                "signedMovePct": _round2(signed_move_pct),
                "moveDirection": move_direction,
                "upBreach": up_breach,
                "downBreach": down_breach,
                "breachSide": breach_side,
                "upOvershootPct": _round2(up_overshoot_pct),
                "downOvershootPct": _round2(down_overshoot_pct),
                "breach": breach,
                "aboveBreachPct": _round2(above_breach_pct),
                "aboveBreachPctVsK": _round2(above_breach_pct_vs_k) if flags.ADD_K_CONSISTENT_OVERSHOOT else None,
                "notes": row_notes,
            }
        )

    # Step 6: summary
    events_found = len(parsed)
    events_used = len(breaches)
    breaches_count = sum(1 for b in breaches if b)

    breach_rate_pct = _mean([1.0 if b else 0.0 for b in breaches])
    breach_rate_pct = None if breach_rate_pct is None else breach_rate_pct * 100.0
    baseline_breach_rate_pct = breach_rate_pct
    baseline_avg_ratio = _mean(ratios_all)
    baseline_avg_above_breach = _mean(above_breach_all)

    up_breach_rate_pct = _rate_pct(up_breaches_all, events_used)
    down_breach_rate_pct = _rate_pct(down_breaches_all, events_used)
    avg_up_overshoot_pct = _mean(up_overshoot_all)
    avg_down_overshoot_pct = _mean(down_overshoot_all)
    tail_bias = _tail_bias(
        up_breach_rate_pct=up_breach_rate_pct,
        down_breach_rate_pct=down_breach_rate_pct,
        avg_up_overshoot_pct=avg_up_overshoot_pct,
        avg_down_overshoot_pct=avg_down_overshoot_pct,
    )

    summary = {
        "events_found": events_found,
        "events_used": events_used,
        "breaches": breaches_count,
        "breach_rate_pct": _round2(breach_rate_pct),
        # Additive display fields: keep raw rates explicit when decisioning uses shrinkage.
        "breachRatePct_raw": _round2(breach_rate_pct),
        "avg_above_breach_pct": _round2(_mean(above_breach_all)),
        # Optional additive k-consistent overshoot summary (vs threshold k*implied, not vs implied).
        "avg_above_breach_pct_vs_k": _round2(_mean(above_breach_vs_k_all)) if flags.ADD_K_CONSISTENT_OVERSHOOT else None,
        "avg_realized_if_breach_pct": _round2(_mean(realized_if_breach)),
        "avg_realized_all_pct": _round2(_mean(realized_all)),
        "avg_implied_all_pct": _round2(_mean(implied_all)),
        # Phase 1 directional aggregates (added keys only)
        "upBreachRatePct": _round2(up_breach_rate_pct),
        "downBreachRatePct": _round2(down_breach_rate_pct),
        "avgUpOvershootPct": _round2(avg_up_overshoot_pct),
        "avgDownOvershootPct": _round2(avg_down_overshoot_pct),
        "upBreaches": int(up_breaches_all),
        "downBreaches": int(down_breaches_all),
        "tailBias": tail_bias,
    }

    summary_decision: Optional[Dict[str, Any]] = None
    if flags.USE_BETA_POSTERIOR_FOR_DECISIONING and events_used >= 0:
        post = beta_posterior_from_counts(
            successes=int(breaches_count),
            trials=int(events_used),
            alpha0=float(flags.BETA_PRIOR_ALPHA),
            beta0=float(flags.BETA_PRIOR_BETA),
        )
        if post is not None:
            lo, hi = post.ci(level=0.90)
            summary_decision = {
                "breachProb_mean_beta": round(float(post.mean), 6),
                "breachProb_ci90": {"lo": round(float(lo), 6), "hi": round(float(hi), 6)},
                "n": int(events_used),
                "prior": {"alpha": float(flags.BETA_PRIOR_ALPHA), "beta": float(flags.BETA_PRIOR_BETA)},
            }

    # Additive telemetry rollups (safe default; does not change existing semantics)
    if flags.ADD_EVENT_SHIFT_TELEMETRY:
        pr_shifts = [e.get("pricingDateShiftDays") for e in out_events if isinstance(e.get("pricingDateShiftDays"), int)]
        rw_shifts = [e.get("realizedWindowShiftDays") for e in out_events if isinstance(e.get("realizedWindowShiftDays"), int)]
        summary["eventsWithPricingDateShift"] = int(sum(1 for v in pr_shifts if v and v > 0))
        summary["eventsWithRealizedWindowShift"] = int(sum(1 for v in rw_shifts if v and v > 0))
        summary["pricingDateShiftDaysMax"] = int(max(pr_shifts)) if pr_shifts else 0
        summary["realizedWindowShiftDaysMax"] = int(max(rw_shifts)) if rw_shifts else 0

    baseline = {
        "events_used": events_used,
        "breach_rate_pct": _round2(baseline_breach_rate_pct),
        "avg_ratio_realized_to_implied": _round2(baseline_avg_ratio),
        "avg_above_breach_pct": _round2(baseline_avg_above_breach),
    }

    quarters: Dict[str, Any] = {}
    for qk, acc in quarter_acc.items():
        eu = int(acc["events_used"])
        breaches_q = int(acc["breaches"])
        br_q = _rate_pct(breaches_q, eu)

        breaches_k1 = int(acc["breaches_k1"])
        br_k1 = _rate_pct(breaches_k1, eu)

        near08 = _rate_pct(int(acc["near_08"]), eu)
        near09 = _rate_pct(int(acc["near_09"]), eu)

        ratios: List[float] = acc["ratios"]
        avg_ratio = _mean(ratios)
        max_ratio = acc["max_ratio"]

        # Seasonality Score vs baseline (computed over the same usable set)
        # breach_delta_pp uses pp units (quarter breach % - baseline breach %)
        breach_delta_pp = None
        if br_q is not None and baseline_breach_rate_pct is not None:
            breach_delta_pp = br_q - baseline_breach_rate_pct

        ratio_delta = None
        if avg_ratio is not None and baseline_avg_ratio is not None:
            ratio_delta = avg_ratio - baseline_avg_ratio

        quarter_avg_above = _mean(acc["above_breach"])
        overshoot_delta_pp = None
        if quarter_avg_above is not None and baseline_avg_above_breach is not None:
            # above breach values are already in percent units; delta is in percentage points
            overshoot_delta_pp = quarter_avg_above - baseline_avg_above_breach

        # Phase 1 directional quarter metrics + deltas vs baseline (pp deltas where sample size allows)
        q_up_rate = _rate_pct(int(acc["up_breaches"]), eu)
        q_down_rate = _rate_pct(int(acc["down_breaches"]), eu)
        q_avg_up_os = _mean(acc["up_overshoot"])
        q_avg_down_os = _mean(acc["down_overshoot"])

        q_up_delta_pp = None
        q_down_delta_pp = None
        q_up_os_delta_pp = None
        q_down_os_delta_pp = None
        if eu >= 3:
            if q_up_rate is not None and up_breach_rate_pct is not None:
                q_up_delta_pp = q_up_rate - up_breach_rate_pct
            if q_down_rate is not None and down_breach_rate_pct is not None:
                q_down_delta_pp = q_down_rate - down_breach_rate_pct
            if q_avg_up_os is not None and avg_up_overshoot_pct is not None:
                q_up_os_delta_pp = q_avg_up_os - avg_up_overshoot_pct
            if q_avg_down_os is not None and avg_down_overshoot_pct is not None:
                q_down_os_delta_pp = q_avg_down_os - avg_down_overshoot_pct

        z_breach = None
        if eu >= 1 and baseline_breach_rate_pct is not None and br_q is not None:
            p0 = baseline_breach_rate_pct / 100.0
            p = br_q / 100.0
            if 0.0 < p0 < 1.0:
                eps = 1e-9
                denom = (p0 * (1.0 - p0) / max(eu, 1)) ** 0.5
                denom = max(denom, eps)  # avoid div-by-zero
                z_breach = (p - p0) / denom

        seasonality_obj = {
            "breach_delta_pp": _round2(breach_delta_pp),
            "ratio_delta": _round2(ratio_delta),
            "overshoot_delta_pp": _round2(overshoot_delta_pp),
            "z_breach": _round2(z_breach),
        }
        if eu < 3:
            seasonality_obj = {"breach_delta_pp": None, "ratio_delta": None, "overshoot_delta_pp": None, "z_breach": None}

        quarters[qk] = {
            "events_total": int(acc["events_total"]),
            "events_used": eu,
            "breaches": breaches_q,
            "breach_rate_pct": _round2(br_q),
            "near_breach_rate_pct": {"0.8": _round2(near08), "0.9": _round2(near09)},
            "avg_ratio_realized_to_implied": _round2(avg_ratio),
            "avg_above_breach_pct": _round2(_mean(acc["above_breach"])),
            "avg_realized_all_pct": _round2(_mean(acc["realized"])),
            "avg_implied_all_pct": _round2(_mean(acc["implied"])),
            "max_ratio_realized_to_implied": _round2(max_ratio),
            # Phase 1 directional quarter fields (added keys only)
            "quarterUpBreachRatePct": _round2(q_up_rate),
            "quarterDownBreachRatePct": _round2(q_down_rate),
            "quarterAvgUpOvershootPct": _round2(q_avg_up_os),
            "quarterAvgDownOvershootPct": _round2(q_avg_down_os),
            "quarterUpBreachDeltaPP": _round2(q_up_delta_pp),
            "quarterDownBreachDeltaPP": _round2(q_down_delta_pp),
            "quarterAvgUpOvershootDeltaPP": _round2(q_up_os_delta_pp),
            "quarterAvgDownOvershootDeltaPP": _round2(q_down_os_delta_pp),
            "seasonality": seasonality_obj,
            "recommendation": _recommendation(
                events_used=eu,
                breach_rate_k1_pct=br_k1,
                near_09_pct=near09,
                avg_ratio=avg_ratio,
                max_ratio=max_ratio,
                breach_delta_pp=seasonality_obj["breach_delta_pp"],
            ),
        }

    # V3/V3.1 overlays (do not affect core breach/seasonality calculations)
    _, regime_validation = compute_regime_backtest_view(client, t, events=out_events)
    regime = compute_regime_overlay(client, t, quarters=quarters, n=n, years=years, k=float(k), today=(today or dt.date.today()))

    # Current snapshot drives "current quarter" selection (used for wingRecommendation and trade builder)
    now = today or dt.date.today()
    current = _current_snapshot(client, ticker=t, as_of_date=_fmt_date(now))
    try:
        cq_date = _parse_date(str(current.get("asOfDate") or "")[:10])
        current_quarter_key = _quarter_key(cq_date)
    except Exception:
        current_quarter_key = _quarter_key(now)

    # --- Benzinga event risk overlay (additive; default OFF) ---
    event_risk: Optional[Dict[str, Any]] = None
    bz_for_event_risk = benzinga_client if (bool(flags.ENABLE_BENZINGA) and bool(flags.BENZINGA_ENABLE_EVENT_RISK)) else None
    if bz_for_event_risk is not None:
        earn_date_next_for_risk: Optional[str] = _fmt_date(next_event_date) if next_event_date is not None else None
        used_estimate_for_risk = False
        if earn_date_next_for_risk is None:
            try:
                bz_ev = benzinga_next_earnings(bz_for_event_risk, ticker=t, now=now, lookahead_days=365)
                if bz_ev is not None and bz_ev.earn_date:
                    earn_date_next_for_risk = str(bz_ev.earn_date)[:10]
            except Exception:
                pass
        if earn_date_next_for_risk is None:
            # If both ORATS-forward and Benzinga calendar are missing, estimate from historical cadence (LOW confidence).
            past = [(d, r) for (d, r) in parsed if d < now]
            if past:
                past.sort(key=lambda x: x[0], reverse=True)
                recent_dates = [d for (d, _) in past[:6]]
                gaps = []
                for i in range(len(recent_dates) - 1):
                    gaps.append((recent_dates[i] - recent_dates[i + 1]).days)
                gaps_sorted = sorted([g for g in gaps if 1 <= g <= 300])
                if gaps_sorted:
                    mid = len(gaps_sorted) // 2
                    cadence = gaps_sorted[mid] if (len(gaps_sorted) % 2 == 1) else int(round((gaps_sorted[mid - 1] + gaps_sorted[mid]) / 2.0))
                else:
                    cadence = 91
                if cadence < 60 or cadence > 130:
                    cadence = 91
                last_earn = recent_dates[0]
                est = last_earn + dt.timedelta(days=cadence)
                while est <= now:
                    est = est + dt.timedelta(days=cadence)
                while est.weekday() >= 5:
                    est = est + dt.timedelta(days=1)
                earn_date_next_for_risk = _fmt_date(est)
                used_estimate_for_risk = True
        event_risk = compute_event_risk_overlay_optional(
            bz_for_event_risk,
            ticker=t,
            as_of_date=str(current.get("asOfDate") or _fmt_date(now))[:10],
            now=now,
            earn_date_next=earn_date_next_for_risk,
        )
        if used_estimate_for_risk and isinstance(event_risk, dict):
            notes = event_risk.get("notes")
            if isinstance(notes, list):
                notes.insert(0, "Earnings date estimated from history (LOW confidence).")

    # Optional: let eventRisk tighten the regime overlay (bounded).
    if bool(flags.BENZINGA_EVENT_RISK_AFFECTS_REGIME) and event_risk is not None:
        regime = apply_event_risk_adjustment(regime=regime, event_risk=event_risk, flags=flags)

    wing_rec = compute_wing_recommendation(
        summary=summary,
        quarters=quarters,
        regime=regime,
        current_quarter_key=current_quarter_key,
        skew_component=None,
    )
    # Optional: uncertainty-aware confidence mapping (flagged).
    if flags.USE_BETA_CI_FOR_CONFIDENCE and summary_decision is not None:
        try:
            ci = summary_decision.get("breachProb_ci90") or {}
            lo = float(ci.get("lo"))
            hi = float(ci.get("hi"))
            wing_rec["confidence"] = _confidence_from_beta_ci(n=int(events_used), lo=lo, hi=hi)
        except Exception:
            pass
    skew_overlay = compute_skew_overlay(
        client,
        ticker=t,
        current_as_of_date=str(current.get("asOfDate") or str(regime.get("asOfDate") or ""))[:10],
        events=out_events,
        target_dte=2,
    )

    out: Dict[str, Any] = {
        "ticker": t,
        "params": {"n": n, "years": years, "k": float(k)},
        "summary": summary,
        "summaryDecision": summary_decision,
        "baseline": baseline,
        "current": current,
        "regime": regime,
        "regimeValidation": regime_validation,
        "quarters": quarters,
        "events": out_events,
        "skipped": skipped,
        "wingRecommendation": wing_rec,
        "skewOverlay": skew_overlay,
    }
    if event_risk is not None:
        out["eventRisk"] = event_risk

    # Progressive enhancement: chain-based strike builder
    if trade_builder_inputs is not None:
        try:
            out["tradeBuilderInputs"] = {k: v for k, v in trade_builder_inputs.items() if v is not None}
            out["tradeBuilder"] = compute_trade_builder(
                client,
                ticker=t,
                as_of_date=str(current.get("asOfDate") or str(regime.get("asOfDate") or ""))[:10],
                inputs=trade_builder_inputs,
                wing_recommendation=wing_rec,
            )
        except Exception as e:
            out["tradeBuilderInputs"] = {k: v for k, v in trade_builder_inputs.items() if v is not None}
            out["tradeBuilder"] = {
                "underlyingPrice": None,
                "expiration": None,
                "modeUsed": str(trade_builder_inputs.get("mode") or "auto"),
                "symmetryUsed": str(trade_builder_inputs.get("symmetry") or "auto"),
                "put": {},
                "call": {},
                "totalCredit": None,
                "notes": [f"Trade builder failed: {type(e).__name__}: {e}"],
            }

    # --- Monte Carlo (additive, default OFF) ---
    if flags.ENABLE_MONTE_CARLO_EARNINGS:
        next_event: Dict[str, Any] = {
            "earnDateNext": None,
            "timingPlanned": None,
            "pricingDatePlanned": None,
            "pricingDateTarget": None,
            "pricingDateAsOf": None,
            "impliedMovePctPlanned": None,
            "impliedMoveSource": None,
            # Additive provenance fields (for robustness + UI transparency)
            "source": None,  # manual_override | orats_snapshot | benzinga | orats_hist | unknown
            "confidence": None,  # HIGH|MED|LOW
            "rawTime": None,  # Benzinga time string if available
            "dateConfirmed": None,  # Benzinga date_confirmed flag if available
            "notes": [],
        }
        try:
            # 0) Manual override (explicit, trader-entered). This is the most direct way to unblock MC when
            # ORATS forward earnings fields are not available on the delayed plan.
            if next_event_override and next_event_override.get("date"):
                od = str(next_event_override.get("date") or "")[:10]
                try:
                    _ = _parse_date(od)
                    next_event["earnDateNext"] = od
                except Exception:
                    next_event["notes"].append("Invalid manual earnings date override (expected YYYY-MM-DD).")
                    od = ""

                timing_override = str(next_event_override.get("timing") or "").strip().upper()
                timing_planned = timing_override if timing_override in ("AMC", "BMO") else "UNK"
                next_event["timingPlanned"] = timing_planned
                next_event["source"] = "manual_override"
                next_event["confidence"] = "HIGH" if timing_planned in ("AMC", "BMO") else "MED"

                asof = str(current.get("asOfDate") or "")[:10] or None
                next_event["pricingDateAsOf"] = asof
                next_event["pricingDatePlanned"] = asof

                pricing_target = None
                if od and timing_planned == "AMC":
                    pricing_target = od
                elif od and timing_planned == "BMO":
                    prior = get_prior_trading_day(client, t, _parse_date(od))
                    pricing_target = str(prior.tradeDate)[:10] if prior and prior.tradeDate else None
                next_event["pricingDateTarget"] = pricing_target
                if pricing_target and asof and pricing_target != asof:
                    next_event["notes"].append(f"Target pricing date={pricing_target}; using latest available ORATS EOD asOf={asof}.")

                # Use the same EOD snapshot implied move currently displayed in the UI.
                implied = _to_float(current.get("impliedMovePct"))
                if implied is not None:
                    next_event["impliedMovePctPlanned"] = _round2(float(implied))
                    next_event["impliedMoveSource"] = "manual_event+current_snapshot"
                    next_event["notes"].append("Manual earnings override used; implied move anchored to ORATS EOD snapshot.")
                else:
                    # Fallback: compute implied from hist/cores on the as-of date (deterministic EOD anchoring).
                    implied_pct_planned: Optional[float] = None
                    used_date: Optional[str] = None
                    if asof:
                        try:
                            cores_date = _parse_date(asof)
                        except Exception:
                            cores_date = None
                        if cores_date is not None:
                            for _ in range(0, 5):
                                try:
                                    cores_resp = client.hist_cores(ticker=t, trade_date=_fmt_date(cores_date), fields="ticker,tradeDate,stockPrice,impErnMv")
                                    row = _first_row(cores_resp.rows)
                                except Exception:
                                    row = None
                                if row and row.get("impErnMv") is not None:
                                    used_date = str(row.get("tradeDate") or _fmt_date(cores_date))[:10]
                                    implied_pct_planned = _imp_to_pct(row.get("impErnMv"))
                                    break
                                cores_date = cores_date - dt.timedelta(days=1)
                    if implied_pct_planned is not None:
                        next_event["impliedMovePctPlanned"] = _round2(implied_pct_planned)
                        next_event["impliedMoveSource"] = "manual_event+hist_cores_asof"
                        if used_date and asof and used_date != asof:
                            next_event["notes"].append(f"Manual override implied move used fallback cores date {used_date} (asOf {asof}).")
                        else:
                            next_event["notes"].append("Manual earnings override used; implied move from ORATS hist/cores on as-of date.")
                    else:
                        next_event["impliedMoveSource"] = "manual_event_missing_implied"
                        next_event["notes"].append("Manual earnings override set, but implied move unavailable from current snapshot and hist/cores.")

            # If manual override provided and valid, skip automatic discovery paths.
            if not next_event["earnDateNext"]:
                # Prefer ORATS snapshot /cores for forward-looking earnings metadata (EOD snapshot).
                snap_fields = "ticker,tradeDate,stockPrice,impErnMv,nextErn,nextErnTod,daysToNextErn,wksNextErn"
                snap_row: Optional[dict] = None
                try:
                    snap = client.cores(ticker=t, fields=snap_fields)
                    snap_row = _first_row(snap.rows) if snap and getattr(snap, "rows", None) is not None else None
                except Exception:
                    snap_row = None

                used_snapshot = False
                if snap_row:
                    next_ern = str(snap_row.get("nextErn") or "")[:10]
                    # ORATS sometimes uses 0000-00-00 when not entitled / not available.
                    if next_ern and next_ern != "0000-00-00":
                        try:
                            nd = _parse_date(next_ern)
                        except Exception:
                            nd = None
                        if nd and nd >= now:
                            used_snapshot = True
                            next_event["earnDateNext"] = next_ern
                            timing_planned = classify_timing(snap_row.get("nextErnTod"))
                            next_event["timingPlanned"] = timing_planned
                            next_event["source"] = "orats_snapshot"
                            next_event["confidence"] = "HIGH" if timing_planned in ("AMC", "BMO") else "MED"
                            asof = str(snap_row.get("tradeDate") or current.get("asOfDate") or "")[:10] or None
                            next_event["pricingDateAsOf"] = asof
                            next_event["pricingDatePlanned"] = asof
                            next_event["impliedMovePctPlanned"] = _round2(_imp_to_pct(snap_row.get("impErnMv")))
                            next_event["impliedMoveSource"] = "cores_snapshot"
                            next_event["notes"].append(f"Anchored to ORATS /cores snapshot asOf={asof}.")

                            # Also compute the theoretical pricing-date target for the event (for transparency).
                            pricing_target: Optional[str] = None
                            if timing_planned == "AMC":
                                pricing_target = next_ern
                            elif timing_planned == "BMO":
                                prior = get_prior_trading_day(client, t, _parse_date(next_ern))
                                pricing_target = str(prior.tradeDate)[:10] if prior and prior.tradeDate else None
                            next_event["pricingDateTarget"] = pricing_target
                        if pricing_target and asof and pricing_target != asof:
                            next_event["notes"].append(f"Target pricing date={pricing_target}; using latest available ORATS EOD asOf={asof}.")

                        if next_event["impliedMovePctPlanned"] is None:
                            next_event["notes"].append("Missing impErnMv in /cores snapshot; will fall back.")
                    else:
                        next_event["notes"].append("ORATS /cores snapshot nextErn is missing or not in the future; falling back.")
                else:
                    next_event["notes"].append("ORATS /cores snapshot nextErn unavailable (possibly subscription-gated); falling back.")

                if not used_snapshot:
                    # Benzinga fallback (calendar) for next earnings date/time.
                    # This hardens MC/trade-builder against ORATS forward-field entitlement gaps.
                    used_benzinga = False
                    bz = benzinga_client if bool(flags.ENABLE_BENZINGA) else None
                    if bz is not None:
                        try:
                            bz_ev = benzinga_next_earnings(bz, ticker=t, now=now, lookahead_days=365)
                        except Exception as e:
                            bz_ev = None
                            next_event["notes"].append(f"Benzinga earnings calendar failed: {type(e).__name__}: {e}")
                        if bz_ev is not None and bz_ev.earn_date:
                            used_benzinga = True
                            next_event["earnDateNext"] = str(bz_ev.earn_date)[:10]
                            next_event["timingPlanned"] = str(bz_ev.timing or "UNK")
                            next_event["source"] = "benzinga"
                            next_event["confidence"] = str(bz_ev.confidence or "LOW")
                            next_event["rawTime"] = bz_ev.raw_time
                            next_event["dateConfirmed"] = bz_ev.date_confirmed
                            next_event["notes"].append("Using Benzinga earnings calendar for next earnings date/time.")

                            # Keep EOD anchoring: pricingDatePlanned stays at latest available ORATS EOD as-of date.
                            asof = str(current.get("asOfDate") or "")[:10] or None
                            next_event["pricingDateAsOf"] = asof
                            next_event["pricingDatePlanned"] = asof

                            # Compute theoretical target pricing date for transparency (may differ from as-of).
                            pricing_target = None
                            if next_event["timingPlanned"] == "AMC":
                                pricing_target = next_event["earnDateNext"]
                            elif next_event["timingPlanned"] == "BMO":
                                try:
                                    prior = get_prior_trading_day(client, t, _parse_date(next_event["earnDateNext"]))
                                    pricing_target = str(prior.tradeDate)[:10] if prior and prior.tradeDate else None
                                except Exception:
                                    pricing_target = None
                            next_event["pricingDateTarget"] = pricing_target
                            if pricing_target and asof and pricing_target != asof:
                                next_event["notes"].append(f"Target pricing date={pricing_target}; using latest available ORATS EOD asOf={asof}.")

                            # Anchor implied move to the same ORATS EOD snapshot shown in the UI.
                            implied = _to_float(current.get("impliedMovePct"))
                            if implied is not None:
                                next_event["impliedMovePctPlanned"] = _round2(float(implied))
                                next_event["impliedMoveSource"] = "benzinga_event+current_snapshot"
                            else:
                                # Fallback: deterministic EOD anchoring via hist/cores on the latest as-of date.
                                next_event["notes"].append("Benzinga event resolved, but current snapshot implied move missing; falling back to ORATS hist/cores as-of.")
                                implied_pct_planned: Optional[float] = None
                                used_date: Optional[str] = None
                                if asof:
                                    try:
                                        cores_date = _parse_date(asof)
                                    except Exception:
                                        cores_date = None
                                    if cores_date is not None:
                                        for _ in range(0, 5):
                                            try:
                                                cores_resp = client.hist_cores(
                                                    ticker=t,
                                                    trade_date=_fmt_date(cores_date),
                                                    fields="ticker,tradeDate,stockPrice,impErnMv",
                                                )
                                                row = _first_row(cores_resp.rows)
                                            except Exception:
                                                row = None
                                            if row and row.get("impErnMv") is not None:
                                                used_date = str(row.get("tradeDate") or _fmt_date(cores_date))[:10]
                                                implied_pct_planned = _imp_to_pct(row.get("impErnMv"))
                                                break
                                            cores_date = cores_date - dt.timedelta(days=1)

                                if implied_pct_planned is not None:
                                    next_event["impliedMovePctPlanned"] = _round2(implied_pct_planned)
                                    next_event["impliedMoveSource"] = "benzinga_event+hist_cores_asof"
                                    if used_date and asof and used_date != asof:
                                        next_event["notes"].append(f"Implied move used fallback cores date {used_date} (asOf {asof}).")
                                else:
                                    next_event["impliedMoveSource"] = "benzinga_event_missing_implied"
                                    next_event["notes"].append("Benzinga event resolved, but implied move unavailable from ORATS current snapshot and hist/cores.")

                    # Fallback path: infer upcoming event from /hist/earnings (may be historical-only on delayed plans).
                    if (not used_benzinga) and next_event_date is not None and next_event_raw is not None:
                        annc_tod = next_event_raw.get("anncTod") or next_event_raw.get("annc_tod") or next_event_raw.get("anncTOD")
                        timing_planned = classify_timing(annc_tod)
                        next_event["earnDateNext"] = _fmt_date(next_event_date)
                        next_event["timingPlanned"] = timing_planned
                        next_event["source"] = next_event.get("source") or "orats_hist"
                        next_event["confidence"] = next_event.get("confidence") or ("MED" if timing_planned in ("AMC", "BMO") else "LOW")

                        pricing_planned: Optional[str] = None
                        if timing_planned == "AMC":
                            pricing_planned = _fmt_date(next_event_date)
                        elif timing_planned == "BMO":
                            prior = get_prior_trading_day(client, t, next_event_date)
                            if prior and prior.tradeDate:
                                pricing_planned = str(prior.tradeDate)[:10]
                            else:
                                next_event["notes"].append("Unable to determine prior trading day for BMO pricing date; falling back to current as-of.")
                        else:
                            next_event["notes"].append("Unknown upcoming earnings timing; pricing date unclear.")

                        if pricing_planned is None:
                            pricing_planned = str(current.get("asOfDate") or "")[:10] or None
                        next_event["pricingDatePlanned"] = pricing_planned
                        next_event["pricingDateAsOf"] = str(current.get("asOfDate") or "")[:10] or None
                        next_event["pricingDateTarget"] = pricing_planned

                        implied_pct_planned: Optional[float] = None
                        implied_source: Optional[str] = None
                        if pricing_planned and timing_planned in ("AMC", "BMO"):
                            cores_date = _parse_date(pricing_planned)
                            used_date: Optional[str] = None
                            for _ in range(0, 5):
                                try:
                                    cores_resp = client.hist_cores(ticker=t, trade_date=_fmt_date(cores_date), fields="ticker,tradeDate,stockPrice,impErnMv")
                                    row = _first_row(cores_resp.rows)
                                except Exception:
                                    row = None
                                if row and row.get("impErnMv") is not None:
                                    used_date = str(row.get("tradeDate") or _fmt_date(cores_date))[:10]
                                    implied_pct_planned = _imp_to_pct(row.get("impErnMv"))
                                    break
                                cores_date = cores_date - dt.timedelta(days=1)
                            if implied_pct_planned is not None:
                                if used_date and used_date == pricing_planned:
                                    implied_source = "cores_on_pricingDate"
                                else:
                                    implied_source = "cores_fallback_prior"
                                    if used_date:
                                        next_event["notes"].append(f"Cores implied move used fallback date {used_date} (planned {pricing_planned}).")
                            else:
                                implied_source = "missing"
                                next_event["notes"].append("Missing cores impErnMv for planned pricing date (after retries).")
                        else:
                            implied_source = "missing"

                        if implied_pct_planned is None:
                            fallback_imp = _to_float(current.get("impliedMovePct"))
                            if fallback_imp is not None:
                                implied_pct_planned = float(fallback_imp)
                                implied_source = "current_snapshot_fallback"
                                next_event["notes"].append("Using current snapshot implied move as fallback (pricing-date cores unavailable).")
                            next_event["impliedMovePctPlanned"] = _round2(implied_pct_planned)
                            next_event["impliedMoveSource"] = implied_source
                    else:
                        # No ORATS-forward event available (delayed plans often behave this way).
                        next_event["notes"].append("No upcoming earnings event found in ORATS hist/earnings.")

                # --- Last-resort fallbacks (to avoid "no anchor" for tickers with missing forward calendars) ---
                # 1) If we still don't have an upcoming earnings date, estimate it from historical cadence.
                if not next_event.get("earnDateNext"):
                    # Use the most recent past earnings and a robust median cadence.
                    past = [(d, r) for (d, r) in parsed if d < now]
                    if past:
                        past.sort(key=lambda x: x[0], reverse=True)
                        recent_dates = [d for (d, _) in past[:6]]
                        gaps = []
                        for i in range(len(recent_dates) - 1):
                            gaps.append((recent_dates[i] - recent_dates[i + 1]).days)
                        gaps_sorted = sorted([g for g in gaps if 1 <= g <= 300])
                        if gaps_sorted:
                            mid = len(gaps_sorted) // 2
                            cadence = gaps_sorted[mid] if (len(gaps_sorted) % 2 == 1) else int(round((gaps_sorted[mid - 1] + gaps_sorted[mid]) / 2.0))
                        else:
                            cadence = 91
                        # Clamp to plausible quarterly cadence.
                        if cadence < 60 or cadence > 130:
                            cadence = 91

                        last_earn = recent_dates[0]
                        est = last_earn + dt.timedelta(days=cadence)
                        while est <= now:
                            est = est + dt.timedelta(days=cadence)

                        # Best-effort: keep it on a weekday (earnings rarely scheduled on weekends).
                        while est.weekday() >= 5:
                            est = est + dt.timedelta(days=1)

                        # Guess timing from recent events (mode of last 4 known timings).
                        timings = []
                        for (d, r) in past[:4]:
                            annc = (r or {}).get("anncTod") or (r or {}).get("annc_tod") or (r or {}).get("anncTOD")
                            tp = classify_timing(annc)
                            if tp in ("AMC", "BMO"):
                                timings.append(tp)
                        timing_guess = timings[0] if timings and timings.count(timings[0]) >= timings.count(timings[-1]) else (timings[-1] if timings else "UNK")

                        next_event["earnDateNext"] = _fmt_date(est)
                        next_event["timingPlanned"] = next_event.get("timingPlanned") or timing_guess
                        next_event["source"] = next_event.get("source") or "estimate"
                        next_event["confidence"] = next_event.get("confidence") or "LOW"
                        next_event["notes"].append(
                            f"No forward earnings calendar available; estimated next earnings date from historical cadence (~{cadence}d). LOW confidence — use override to confirm."
                        )

                # 2) Ensure we always have an EOD anchor date for MC (pricingDatePlanned).
                asof_cur = str(current.get("asOfDate") or "")[:10] or None
                if next_event.get("pricingDateAsOf") is None:
                    next_event["pricingDateAsOf"] = asof_cur
                if next_event.get("pricingDatePlanned") is None:
                    next_event["pricingDatePlanned"] = asof_cur

                # If we have a (real or estimated) earnings date + timing, compute a pricing-date target for transparency.
                if next_event.get("pricingDateTarget") is None and next_event.get("earnDateNext") and next_event.get("timingPlanned") in ("AMC", "BMO"):
                    try:
                        ed = _parse_date(str(next_event.get("earnDateNext")))
                    except Exception:
                        ed = None
                    if ed is not None:
                        if next_event.get("timingPlanned") == "AMC":
                            next_event["pricingDateTarget"] = _fmt_date(ed)
                        else:  # BMO
                            prior = get_prior_trading_day(client, t, ed)
                            next_event["pricingDateTarget"] = str(prior.tradeDate)[:10] if prior and prior.tradeDate else None

                # 3) If implied move is still missing, anchor from ORATS hist/cores as-of (deterministic EOD).
                if next_event.get("impliedMovePctPlanned") is None and asof_cur:
                    implied_pct_planned: Optional[float] = None
                    used_date: Optional[str] = None
                    try:
                        cores_date = _parse_date(asof_cur)
                    except Exception:
                        cores_date = None
                    if cores_date is not None:
                        for _ in range(0, 5):
                            try:
                                cores_resp = client.hist_cores(
                                    ticker=t,
                                    trade_date=_fmt_date(cores_date),
                                    fields="ticker,tradeDate,stockPrice,impErnMv",
                                )
                                row = _first_row(cores_resp.rows)
                            except Exception:
                                row = None
                            if row and row.get("impErnMv") is not None:
                                used_date = str(row.get("tradeDate") or _fmt_date(cores_date))[:10]
                                implied_pct_planned = _imp_to_pct(row.get("impErnMv"))
                                break
                            cores_date = cores_date - dt.timedelta(days=1)

                    if implied_pct_planned is not None:
                        next_event["impliedMovePctPlanned"] = _round2(implied_pct_planned)
                        next_event["impliedMoveSource"] = next_event.get("impliedMoveSource") or "hist_cores_asof_only"
                        if used_date and asof_cur and used_date != asof_cur:
                            next_event["notes"].append(f"Implied move anchored from ORATS hist/cores fallback date {used_date} (asOf {asof_cur}).")
                        else:
                            next_event["notes"].append("Implied move anchored from ORATS hist/cores on as-of date.")
                    else:
                        next_event["impliedMoveSource"] = next_event.get("impliedMoveSource") or "missing"
        except Exception as e:
            next_event["notes"].append(f"nextEvent calculation failed: {type(e).__name__}: {e}")
        finally:
            # Always return nextEvent (even if partially populated) so UI/MC can explain failures.
            out["nextEvent"] = next_event

        try:
            out["monteCarlo"] = run_monte_carlo(
                ticker=t,
                params=out.get("params") or {"n": n, "years": years, "k": float(k)},
                flags=flags,
                current=current,
                next_event=next_event,
                regime=regime,
                wing_recommendation=wing_rec,
                events=out_events,
                trade_builder=(out.get("tradeBuilder") if isinstance(out.get("tradeBuilder"), dict) else None),
                event_risk=event_risk,
            )
        except Exception as e:
            out["monteCarlo"] = {"nSims": 0, "notes": [f"MC failed: {type(e).__name__}: {e}"]}

        if flags.MC_ENABLE_TAS_STABILITY:
            try:
                out["stability"] = bootstrap_tas_stability(flags=flags, summary=summary, regime=regime, events=out_events, n_boot=int(flags.MC_BOOTSTRAP_N))
            except Exception as e:
                out["stability"] = {"notes": [f"stability failed: {type(e).__name__}: {e}"]}

        if flags.MC_ENABLE_WING_OPTIMIZATION:
            try:
                out["monteCarloOptimization"] = optimize_wings_risk_only(
                    ticker=t,
                    params=out.get("params") or {"n": n, "years": years, "k": float(k)},
                    flags=flags,
                    current=current,
                    next_event=next_event,
                    regime=regime,
                    wing_recommendation=wing_rec,
                    events=out_events,
                    stability=(out.get("stability") if isinstance(out.get("stability"), dict) else None),
                    event_risk=event_risk,
                )
            except Exception as e:
                out["monteCarloOptimization"] = {"mode": "RISK_ONLY", "notes": [f"Optimization failed: {type(e).__name__}: {e}"]}

    return out


