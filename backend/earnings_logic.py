from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.orats_client import OratsClient, OratsError
from backend.regime_overlay import compute_regime_backtest_view, compute_regime_overlay
from backend.skew_overlay import compute_skew_overlay
from backend.wing_recommendation import compute_wing_recommendation


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


def get_prior_trading_day(client: OratsClient, ticker: str, date_: dt.date, max_steps: int = 10) -> Optional[DailyBar]:
    return find_trading_day(lambda d: fetch_daily_bar(client, ticker, d), date_ - dt.timedelta(days=1), -1, max_steps)


def get_next_trading_day(client: OratsClient, ticker: str, date_: dt.date, max_steps: int = 10) -> Optional[DailyBar]:
    return find_trading_day(lambda d: fetch_daily_bar(client, ticker, d), date_ + dt.timedelta(days=1), +1, max_steps)


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


def compute_breach_stats(
    client: OratsClient,
    ticker: str,
    n: int = 20,
    years: int = 5,
    k: float = 1.0,
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

    # Step 1: earnings events
    earn_resp = client.hist_earnings(t)
    events_raw = earn_resp.rows
    parsed: List[Tuple[dt.date, dict]] = []
    for r in events_raw:
        ed = r.get("earnDate") or r.get("earn_date") or r.get("date")
        if not ed:
            continue
        try:
            d = _parse_date(str(ed))
        except ValueError:
            continue
        parsed.append((d, r))

    parsed.sort(key=lambda x: x[0], reverse=True)
    cutoff = dt.date.today() - dt.timedelta(days=365 * years)
    parsed = [(d, r) for (d, r) in parsed if d >= cutoff]
    parsed = parsed[:n]
    current_quarter_key = _quarter_key(parsed[0][0]) if parsed else None

    # Step 2-5: per-event computations
    out_events: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    implied_all: List[float] = []
    realized_all: List[float] = []
    breaches: List[bool] = []
    above_breach_all: List[float] = []
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
        up_breach: Optional[bool] = None
        down_breach: Optional[bool] = None
        breach_side: Optional[str] = None
        up_overshoot_pct: Optional[float] = None
        down_overshoot_pct: Optional[float] = None

        # Determine pricing date and realized window dates per spec
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

        else:
            # Spec: either fallback close(prior)->open(next) OR mark unknown timing and skip breach calc.
            row_notes.append("unknown timing (anncTod); excluded from breach stats")
            if prior_bar and prior_bar.clsPx is not None:
                close_date_used = prior_bar.tradeDate
                close_px = prior_bar.clsPx
            if next_bar and next_bar.open is not None:
                open_date_used = next_bar.tradeDate
                open_px = next_bar.open

        # Step 3: implied move from cores using pricing_date_used
        if timing in ("AMC", "BMO") and pricing_date_used:
            # if cores missing for date, retry with nearest prior trading day (max 5)
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
        if valid_for_stats:
            implied_k = float(implied_pct) * float(k)
            breach = realized_pct > implied_k
            if breach and implied_pct and implied_pct > 0:
                above_breach_pct = (realized_pct - implied_pct) / implied_pct * 100.0

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
            reason = "unknown timing" if timing == "UNK" else "missing implied/realized data"
            skipped.append({"earnDate": _fmt_date(earn_date), "reason": reason})

        out_events.append(
            {
                "earnDate": _fmt_date(earn_date),
                "anncTod": None if annc_tod is None else str(annc_tod),
                "timing": timing,
                "pricingDateUsed": pricing_date_used,
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
        "avg_above_breach_pct": _round2(_mean(above_breach_all)),
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
    regime = compute_regime_overlay(client, t, quarters=quarters, n=n, years=years, k=float(k))
    wing_rec = compute_wing_recommendation(
        summary=summary,
        quarters=quarters,
        regime=regime,
        current_quarter_key=current_quarter_key,
        skew_component=None,
    )
    skew_overlay = compute_skew_overlay(
        client,
        ticker=t,
        current_as_of_date=str(regime.get("asOfDate") or "")[:10],
        events=out_events,
        target_dte=2,
    )

    return {
        "ticker": t,
        "params": {"n": n, "years": years, "k": float(k)},
        "summary": summary,
        "baseline": baseline,
        "regime": regime,
        "regimeValidation": regime_validation,
        "quarters": quarters,
        "events": out_events,
        "skipped": skipped,
        "wingRecommendation": wing_rec,
        "skewOverlay": skew_overlay,
    }


