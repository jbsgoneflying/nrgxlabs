"""Live data adapters — the ONLY place the harness touches the network.

These wrap the existing app clients (EODHD / ORATS / API Ninjas) and translate
their responses into the harness dataclasses. They are used by the CLI runners
to produce real reports; the core logic and unit tests never import this module.

Provider choices (and why):
  * **Prices -> EODHD** ``get_eod`` (one call per ticker for a full date range;
    uses adjusted_close for split/dividend continuity). ORATS ``hist_dailies`` is
    per-date and would be hundreds of calls per name — wrong tool here.
  * **Earnings -> EODHD** ``get_calendar_earnings`` (actual vs estimate EPS +
    BMO/AMC timing, historical).
  * **Insider -> API Ninjas** ``get_insider_transactions`` (Form 4, already wired
    in the app).

Requires env: ``EODHD_API_TOKEN``, ``API_NINJAS_API_KEY`` (ORATS optional).
"""
from __future__ import annotations

import logging
from typing import List, Optional

from backend.research.data_provider import (
    EarningsEvent,
    InsiderTxn,
    OptionQuote,
    PriceBar,
)

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Symbol helpers
# ---------------------------------------------------------------------------

def to_eodhd_symbol(ticker: str) -> str:
    """Map a plain US ticker to EODHD format (e.g. BRK.B -> BRK-B.US)."""
    t = ticker.strip().upper()
    if "." in t and not t.endswith(".US"):
        t = t.replace(".", "-")
    if not t.endswith(".US"):
        t = f"{t}.US"
    return t


def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if x == x else None  # drop NaN
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Price provider (EODHD EOD)
# ---------------------------------------------------------------------------

class EodhdPriceProvider:
    def __init__(self, client=None) -> None:
        if client is None:
            from backend.eodhd_client import EodhdClient

            client = EodhdClient.from_env()
        self._client = client

    def get_bars(self, ticker: str, start: str, end: str) -> List[PriceBar]:
        sym = to_eodhd_symbol(ticker)
        resp = self._client.get_eod(sym, from_date=start[:10], to_date=end[:10])
        bars: List[PriceBar] = []
        for row in resp.rows or []:
            date = str(row.get("date") or "")[:10]
            if not date:
                continue
            close = _f(row.get("adjusted_close"))
            raw_close = _f(row.get("close"))
            if close is None:
                close = raw_close
            o, h, l = _f(row.get("open")), _f(row.get("high")), _f(row.get("low"))
            # When using adjusted_close, scale OHLC by the same adj factor so
            # intraday fields stay consistent with the adjusted close.
            if close is not None and raw_close not in (None, 0) and raw_close != close:
                factor = close / raw_close
                o = o * factor if o is not None else None
                h = h * factor if h is not None else None
                l = l * factor if l is not None else None
            if close is None:
                continue
            bars.append(
                PriceBar(
                    date=date,
                    open=o if o is not None else close,
                    high=h if h is not None else close,
                    low=l if l is not None else close,
                    close=close,
                    volume=_f(row.get("volume")) or 0.0,
                )
            )
        bars.sort(key=lambda b: b.date)
        return bars


# ---------------------------------------------------------------------------
# Earnings provider (EODHD calendar)
# ---------------------------------------------------------------------------

class EodhdEarningsProvider:
    _TIMING_MAP = {
        "BeforeMarket": "bmo",
        "AfterMarket": "amc",
        "before market": "bmo",
        "after market": "amc",
    }

    def __init__(self, client=None) -> None:
        if client is None:
            from backend.eodhd_client import EodhdClient

            client = EodhdClient.from_env()
        self._client = client

    def get_events(self, ticker: str, start: str, end: str) -> List[EarningsEvent]:
        sym = to_eodhd_symbol(ticker)
        resp = self._client.get_calendar_earnings(
            symbols=sym, from_date=start[:10], to_date=end[:10]
        )
        events: List[EarningsEvent] = []
        for row in resp.rows or []:
            date = str(row.get("report_date") or row.get("date") or "")[:10]
            if not date or not (start <= date <= end):
                continue
            timing_raw = str(row.get("before_after_market") or "").strip()
            timing = self._TIMING_MAP.get(timing_raw, "")
            events.append(
                EarningsEvent(
                    ticker=ticker.upper(),
                    report_date=date,
                    timing=timing,
                    actual_eps=_f(row.get("actual")),
                    estimate_eps=_f(row.get("estimate")),
                )
            )
        events.sort(key=lambda e: e.report_date)
        return events


# ---------------------------------------------------------------------------
# Insider provider (API Ninjas Form 4)
# ---------------------------------------------------------------------------

class ApiNinjasInsiderProvider:
    def __init__(self, client=None) -> None:
        if client is None:
            from backend.api_ninjas_client import ApiNinjasClient

            client = ApiNinjasClient.from_env()
        self._client = client

    def get_transactions(self, ticker: str, start: str, end: str) -> List[InsiderTxn]:
        # API Ninjas returns newest-first pages of 100; paginate back until we
        # pass the start date (or hit the page cap).
        rows: List[dict] = []
        for page in range(10):
            batch = self._client.get_insider_transactions(ticker, limit=100, offset=page * 100)
            if not batch:
                break
            rows.extend(batch)
            oldest = min(
                (str(r.get("filing_date") or r.get("transaction_date") or "9999"))[:10]
                for r in batch
            )
            if oldest < start:
                break
        txns: List[InsiderTxn] = []
        for row in rows or []:
            filing = str(row.get("filing_date") or row.get("transaction_date") or "")[:10]
            if not filing or not (start <= filing <= end):
                continue
            raw_type = str(row.get("transaction_type") or "").strip().lower()
            # API Ninjas taxonomy: only bare "purchase" is an open-market buy
            # (the signal). derivative_purchase / exercise / award are comp
            # plumbing, frequently at price=0 — excluded to avoid false clusters.
            if raw_type in ("purchase", "buy", "p"):
                side = "buy"
            elif raw_type in ("sale", "sell", "s", "derivative_sale", "other_disposition"):
                side = "sell"
            else:
                continue
            txns.append(
                InsiderTxn(
                    ticker=ticker.upper(),
                    filing_date=filing,
                    trade_date=str(row.get("transaction_date") or row.get("trade_date") or filing)[:10],
                    owner=str(row.get("owner_name") or row.get("owner") or "unknown"),
                    side=side,
                    shares=_f(row.get("shares_traded")) or _f(row.get("amount")) or 0.0,
                    price=_f(row.get("price")) or _f(row.get("share_price")) or 0.0,
                )
            )
        txns.sort(key=lambda t: t.filing_date)
        return txns


# ---------------------------------------------------------------------------
# Transcript context (API Ninjas) — for the PEAD quality overlay (Pass B)
# ---------------------------------------------------------------------------

class ApiNinjasTranscriptProvider:
    """Fetch earnings-call transcript text near a report date (best-effort).

    API Ninjas keys transcripts by (year, quarter); we try the calendar quarter
    of the report date and its neighbors and return the first hit's text.
    """

    def __init__(self, client=None) -> None:
        if client is None:
            from backend.api_ninjas_client import ApiNinjasClient

            client = ApiNinjasClient.from_env()
        self._client = client

    def get_text(self, ticker: str, report_date: str) -> str:
        year = int(report_date[:4])
        month = int(report_date[5:7])
        cal_q = (month - 1) // 3 + 1
        # Reports usually cover the *prior* fiscal quarter; try a few candidates.
        candidates = [
            (year, cal_q),
            (year, cal_q - 1 if cal_q > 1 else 4),
            (year - 1 if cal_q == 1 else year, 4 if cal_q == 1 else cal_q - 1),
        ]
        for y, q in candidates:
            if q < 1 or q > 4:
                continue
            try:
                t = self._client.get_transcript(ticker, y, q)
            except Exception:
                t = None
            if t and t.get("transcript"):
                return str(t.get("transcript"))
        return ""


# ---------------------------------------------------------------------------
# News context (EODHD) — for the residual-reversal news veto (Pass B)
# ---------------------------------------------------------------------------

class OratsChainProvider:
    """Historical option chains via ORATS ``hist_strikes`` — for convexity pilot.

    Each ORATS strike row carries both the call and put, so we emit two
    OptionQuotes per row. Mids are (bid+ask)/2. Coverage on small-caps is the
    documented weak point of the convexity pilot.
    """

    def __init__(self, client=None, dte_range: str = "1,120") -> None:
        if client is None:
            from backend.deps import get_client

            client = get_client()
        self._client = client
        self._dte_range = dte_range

    def get_chain(self, ticker: str, trade_date: str):
        fields = (
            "expirDate,dte,strike,callBidPrice,callAskPrice,callValue,"
            "putBidPrice,putAskPrice,putValue,delta"
        )
        resp = self._client.hist_strikes(
            ticker=ticker.upper(), trade_date=trade_date[:10], fields=fields, dte=self._dte_range
        )
        out = []
        for r in resp.rows or []:
            expiry = str(r.get("expirDate") or "")[:10]
            strike = _f(r.get("strike"))
            dte = _f(r.get("dte"))
            if not expiry or strike is None:
                continue
            call_mid = _mid(r.get("callBidPrice"), r.get("callAskPrice"), r.get("callValue"))
            put_mid = _mid(r.get("putBidPrice"), r.get("putAskPrice"), r.get("putValue"))
            delta = _f(r.get("delta")) or 0.0
            if call_mid is not None:
                out.append(OptionQuote(expiry, strike, "C", call_mid, int(dte or 0), delta))
            if put_mid is not None:
                out.append(OptionQuote(expiry, strike, "P", put_mid, int(dte or 0), delta))
        return out


def _mid(bid, ask, fallback=None):
    b, a = _f(bid), _f(ask)
    if b is not None and a is not None and a >= b >= 0:
        return (a + b) / 2.0
    return _f(fallback)


class EodhdNewsProvider:
    """Detect whether fresh news exists in a window before a signal date."""

    def __init__(self, client=None) -> None:
        if client is None:
            from backend.eodhd_client import EodhdClient

            client = EodhdClient.from_env()
        self._client = client

    def has_fresh_news(self, ticker: str, signal_date: str, lookback_days: int = 3) -> bool:
        import datetime as dt

        sym = to_eodhd_symbol(ticker)
        start = (dt.date.fromisoformat(signal_date[:10]) - dt.timedelta(days=lookback_days)).isoformat()
        try:
            resp = self._client.get_news(ticker=sym, from_date=start, to_date=signal_date[:10], limit=10)
            return bool(resp.rows)
        except Exception:
            return False
