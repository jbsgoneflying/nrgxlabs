"""Engine 18 ingest — fresh earnings reports, transcripts, liquidity.

One bulk EODHD calendar call covers the whole window (all symbols), then we
filter to the desk universe (S&P 500 + NASDAQ 100). Transcripts come from
API Ninjas keyed by (year, quarter); liquidity from EODHD daily bars.

Point-in-time discipline (matches the validated backtest exactly): entry is
the **next session open after the report date**, regardless of BMO/AMC timing.
A BMO report graded the same morning therefore points at tomorrow's open —
conservative by construction, identical to how the +0.65%/trade edge was
measured.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Dict, List, Optional, Tuple

from backend.engine18.models import EarningsReport

LOG = logging.getLogger(__name__)

_TIMING_MAP = {
    "BeforeMarket": "bmo",
    "AfterMarket": "amc",
    "before market": "bmo",
    "after market": "amc",
}


def _eodhd_optional():
    try:
        from backend.eodhd_client import EodhdClient

        return EodhdClient.from_env()
    except Exception:
        return None


def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if x == x else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _plain_ticker(code: str) -> str:
    """EODHD code (BRK-B.US) -> desk ticker (BRK.B)."""
    t = str(code or "").upper().strip()
    if t.endswith(".US"):
        t = t[:-3]
    return t.replace("-", ".")


def to_eodhd_symbol(ticker: str) -> str:
    t = ticker.strip().upper()
    if "." in t and not t.endswith(".US"):
        t = t.replace(".", "-")
    if not t.endswith(".US"):
        t = f"{t}.US"
    return t


def load_universe() -> List[str]:
    """Desk universe: S&P 500 + NASDAQ 100 (deduped, sorted)."""
    try:
        from backend.research.universe import load_nasdaq100, load_sp500

        return sorted(set(load_sp500()) | set(load_nasdaq100()))
    except Exception as exc:
        LOG.warning("engine18: universe load failed: %s", exc)
        return []


def fetch_recent_reports(
    *,
    lookback_days: int,
    as_of: Optional[dt.date] = None,
    client=None,
    universe: Optional[List[str]] = None,
) -> List[EarningsReport]:
    """Reports with actual EPS in [as_of - lookback_days, as_of], universe only."""
    client = client or _eodhd_optional()
    if client is None:
        LOG.warning("engine18: EODHD client unavailable")
        return []
    today = as_of or dt.date.today()
    start = (today - dt.timedelta(days=int(lookback_days))).isoformat()
    end = today.isoformat()
    uni = set(universe if universe is not None else load_universe())

    try:
        resp = client.get_calendar_earnings(from_date=start, to_date=end)
        rows = resp.rows or []
    except Exception as exc:
        LOG.warning("engine18: calendar fetch failed: %s", exc)
        return []

    out: List[EarningsReport] = []
    seen: set = set()
    for row in rows:
        ticker = _plain_ticker(row.get("code"))
        if not ticker or (uni and ticker not in uni):
            continue
        date = str(row.get("report_date") or row.get("date") or "")[:10]
        if not date or not (start <= date <= end):
            continue
        actual = _f(row.get("actual"))
        estimate = _f(row.get("estimate"))
        if actual is None or estimate is None:
            continue  # actual not out yet — nothing to drift on
        key = (ticker, date)
        if key in seen:
            continue
        seen.add(key)
        surprise = None
        if estimate != 0:
            surprise = (actual - estimate) / abs(estimate)
        timing = _TIMING_MAP.get(str(row.get("before_after_market") or "").strip(), "")
        out.append(
            EarningsReport(
                ticker=ticker,
                report_date=date,
                timing=timing,
                actual_eps=actual,
                estimate_eps=estimate,
                surprise_pct=surprise,
            )
        )
    out.sort(key=lambda r: (r.report_date, r.ticker))
    LOG.info("engine18: %d in-universe reports with actuals in %s..%s", len(out), start, end)
    return out


def _fmp_optional():
    try:
        from backend.fmp_client import FmpClient

        return FmpClient.from_env()
    except Exception:
        return None


def _report_from_eodhd_row(ticker: str, row: dict) -> Optional[EarningsReport]:
    date = str(row.get("report_date") or row.get("date") or "")[:10]
    actual = _f(row.get("actual"))
    estimate = _f(row.get("estimate"))
    if not date or actual is None or estimate is None:
        return None
    surprise = (actual - estimate) / abs(estimate) if estimate != 0 else None
    timing = _TIMING_MAP.get(str(row.get("before_after_market") or "").strip(), "")
    return EarningsReport(
        ticker=ticker, report_date=date, timing=timing,
        actual_eps=actual, estimate_eps=estimate, surprise_pct=surprise,
    )


def _report_from_fmp_row(ticker: str, row: dict) -> Optional[EarningsReport]:
    date = str(row.get("date") or "")[:10]
    actual = _f(row.get("epsActual") if "epsActual" in row else row.get("eps"))
    estimate = _f(row.get("epsEstimated"))
    if not date or actual is None or estimate is None:
        return None
    surprise = (actual - estimate) / abs(estimate) if estimate != 0 else None
    timing = str(row.get("time") or "").strip().lower()
    if timing not in ("bmo", "amc"):
        timing = ""
    return EarningsReport(
        ticker=ticker, report_date=date, timing=timing,
        actual_eps=actual, estimate_eps=estimate, surprise_pct=surprise,
    )


def fetch_report_for_ticker(
    ticker: str,
    *,
    window_days: int = 10,
    as_of: Optional[dt.date] = None,
    client=None,
    fmp_client=None,
    overrides: Optional[dict] = None,
) -> Tuple[Optional[EarningsReport], str, str]:
    """On-demand single-ticker report lookup for the manual PEAD profile.

    Source order: EODHD calendar -> FMP calendar (posts actuals faster) ->
    agent-supplied EPS overrides (last resort while vendors lag the print).
    Returns ``(report | None, eps_source, reason)``. No universe gate — the
    desk may profile any liquid US name (the ADV floor still applies later).
    """
    ticker = str(ticker or "").strip().upper()
    today = as_of or dt.date.today()
    start = (today - dt.timedelta(days=int(window_days))).isoformat()
    end = (today + dt.timedelta(days=1)).isoformat()
    saw_dateonly_row = False

    client = client or _eodhd_optional()
    if client is not None:
        try:
            resp = client.get_calendar_earnings(
                symbols=to_eodhd_symbol(ticker), from_date=start, to_date=end
            )
            rows = [
                row for row in (resp.rows or [])
                if _plain_ticker(row.get("code")) == ticker
            ]
        except Exception as exc:
            LOG.warning("engine18: profile EODHD fetch failed for %s: %s", ticker, exc)
            rows = []
        reports = [r for r in (_report_from_eodhd_row(ticker, row) for row in rows) if r]
        saw_dateonly_row = bool(rows) and not reports
        if reports:
            reports.sort(key=lambda r: r.report_date)
            return reports[-1], "eodhd", ""

    fmp = fmp_client or _fmp_optional()
    if fmp is not None:
        try:
            resp = fmp.earnings_calendar(date_from=start, date_to=end)
            rows = [
                row for row in (resp.rows or [])
                if str(row.get("symbol") or "").strip().upper() == ticker
            ]
        except Exception as exc:
            LOG.warning("engine18: profile FMP fetch failed for %s: %s", ticker, exc)
            rows = []
        reports = [r for r in (_report_from_fmp_row(ticker, row) for row in rows) if r]
        if reports:
            reports.sort(key=lambda r: r.report_date)
            return reports[-1], "fmp", ""

    ov = overrides or {}
    actual = _f(ov.get("actual_eps"))
    estimate = _f(ov.get("estimate_eps"))
    if actual is not None and estimate is not None:
        date = str(ov.get("report_date") or today.isoformat())[:10]
        timing = str(ov.get("timing") or "").strip().lower()
        if timing not in ("bmo", "amc"):
            timing = ""
        surprise = (actual - estimate) / abs(estimate) if estimate != 0 else None
        return (
            EarningsReport(
                ticker=ticker, report_date=date, timing=timing,
                actual_eps=actual, estimate_eps=estimate, surprise_pct=surprise,
            ),
            "manual",
            "",
        )

    if saw_dateonly_row:
        reason = (
            f"{ticker} has a calendar entry but no actual EPS posted yet — "
            "vendors may be lagging the print. Re-try later or enter the "
            "actual/estimate EPS from the press release."
        )
    else:
        reason = (
            f"No earnings report found for {ticker} in the last "
            f"{int(window_days)} days. Check the ticker, or enter the "
            "report EPS manually."
        )
    return None, "", reason


def fetch_liquidity(
    ticker: str, *, client=None, lookback_days: int = 45
) -> Tuple[Optional[float], Optional[float]]:
    """Return (avg daily $ volume over ~30 sessions, last close)."""
    client = client or _eodhd_optional()
    if client is None:
        return None, None
    end = dt.date.today().isoformat()
    start = (dt.date.today() - dt.timedelta(days=int(lookback_days))).isoformat()
    try:
        resp = client.get_eod(to_eodhd_symbol(ticker), from_date=start, to_date=end)
        rows = resp.rows or []
    except Exception as exc:
        LOG.debug("engine18: liquidity fetch failed for %s: %s", ticker, exc)
        return None, None
    dollar_vols: List[float] = []
    last_close: Optional[float] = None
    for row in rows:
        close = _f(row.get("close"))
        vol = _f(row.get("volume"))
        if close is not None:
            last_close = close
        if close is not None and vol is not None:
            dollar_vols.append(close * vol)
    if not dollar_vols:
        return None, last_close
    return sum(dollar_vols) / len(dollar_vols), last_close


def fetch_transcript(ticker: str, report_date: str, *, provider=None) -> str:
    """Earnings-call transcript text near the report date (best-effort, '' if none)."""
    if provider is None:
        try:
            from backend.research.live_providers import ApiNinjasTranscriptProvider

            provider = ApiNinjasTranscriptProvider()
        except Exception as exc:
            LOG.debug("engine18: transcript provider unavailable: %s", exc)
            return ""
    try:
        return provider.get_text(ticker, report_date) or ""
    except Exception as exc:
        LOG.debug("engine18: transcript fetch failed for %s: %s", ticker, exc)
        return ""


def fetch_regime_context() -> Optional[str]:
    """Current market regime label — display context ONLY, never a gate."""
    try:
        from backend.redis_store import get_store_optional

        store = get_store_optional()
        if store is None:
            return None
        for key in ("market_intel:regime:latest", "mi:regime:latest"):
            doc = store.get_json(key)
            if isinstance(doc, dict):
                label = doc.get("label") or doc.get("regime") or doc.get("state")
                if label:
                    return str(label)
    except Exception:
        pass
    return None
