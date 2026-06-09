"""Engine 18 — pipeline tests with fake providers and a fake store.

Exercises the full ingest -> grade -> score -> persist flow without network:
fake EODHD client (calendar + bars), fake transcript provider, injected LLM
function, and an in-memory store standing in for Redis.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict

from backend.config import get_flags
from backend.engine18 import pipeline
from backend.engine18.ingest import fetch_recent_reports

FLAGS = get_flags()

AS_OF = dt.date(2026, 6, 9)  # Tuesday


class _Resp:
    def __init__(self, rows):
        self.rows = rows


class FakeEodhd:
    """Calendar + EOD bars for three names: large beat, small beat, miss."""

    def __init__(self, *, thin_ticker=None):
        self._thin = thin_ticker

    def get_calendar_earnings(self, *, from_date=None, to_date=None, symbols=None):
        rows = [
            # Large beat (+30%), AMC yesterday.
            {"code": "BIGB.US", "report_date": "2026-06-08", "before_after_market": "AfterMarket",
             "actual": 1.30, "estimate": 1.00},
            # Small beat (+8%), BMO today.
            {"code": "SMLB.US", "report_date": "2026-06-09", "before_after_market": "BeforeMarket",
             "actual": 1.08, "estimate": 1.00},
            # Miss (-20%) — must never become a candidate.
            {"code": "MISS.US", "report_date": "2026-06-08", "before_after_market": "AfterMarket",
             "actual": 0.80, "estimate": 1.00},
            # Beat outside the universe — filtered.
            {"code": "XUNI.US", "report_date": "2026-06-08", "actual": 2.0, "estimate": 1.0},
            # Estimate not out yet — skipped.
            {"code": "BIGB2.US", "report_date": "2026-06-09", "actual": None, "estimate": 1.0},
        ]
        return _Resp(rows)

    def get_eod(self, symbol, *, from_date=None, to_date=None):
        ticker = symbol.replace(".US", "").replace("-", ".")
        # Thin name: $50k/day — below any sane ADV floor.
        px, vol = (5.0, 10_000) if ticker == self._thin else (100.0, 5_000_000)
        rows = [
            {"date": f"2026-05-{d:02d}", "close": px, "volume": vol}
            for d in range(11, 30)
        ]
        return _Resp(rows)


class FakeTranscripts:
    def get_text(self, ticker, report_date):
        if ticker == "BIGB":
            return "we raise full year guidance, record demand, strong growth and momentum"
        return ""  # SMLB: no transcript -> neutral grade


class FakeStore:
    """Minimal RedisStore stand-in (get_json/set_json/scan_keys)."""

    def __init__(self):
        self.data: Dict[str, Any] = {}

    def get_json(self, key):
        return self.data.get(key)

    def set_json(self, key, value, ttl_s=0):
        self.data[key] = value
        return True

    def scan_keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in self.data if k.startswith(prefix)]


UNIVERSE = ["BIGB", "SMLB", "MISS", "BIGB2"]


def _llm(text):
    return (0.9, "Guidance raised; tone strongly bullish.")


def test_fetch_recent_reports_filters_and_surprises():
    reports = fetch_recent_reports(
        lookback_days=3, as_of=AS_OF, client=FakeEodhd(), universe=UNIVERSE,
    )
    tickers = {r.ticker for r in reports}
    assert tickers == {"BIGB", "SMLB", "MISS"}  # XUNI out-of-universe, BIGB2 no actual
    big = next(r for r in reports if r.ticker == "BIGB")
    assert abs(big.surprise_pct - 0.30) < 1e-9
    assert big.timing == "amc"


def test_build_scan_end_to_end():
    store = FakeStore()
    payload = pipeline.build_scan(
        flags=FLAGS,
        eodhd_client=FakeEodhd(),
        transcript_provider=FakeTranscripts(),
        llm_fn=_llm,
        store=store,
        as_of=AS_OF,
        universe=UNIVERSE,
    )

    # Misses never appear; both beats become candidates.
    tickers = [c["ticker"] for c in payload["candidates"]]
    assert "MISS" not in tickers
    assert set(tickers) == {"BIGB", "SMLB"}

    big = next(c for c in payload["candidates"] if c["ticker"] == "BIGB")
    assert big["bucket"] == "beat_large"
    assert big["grade"]["source"] == "llm"
    assert big["grade"]["score"] == 0.9
    # 2026-06-08 report -> 2026-06-09 entry -> 2026-06-23 exit (10 td).
    assert big["entry_date"] == "2026-06-09"
    assert big["exit_date"] == "2026-06-23"

    sml = next(c for c in payload["candidates"] if c["ticker"] == "SMLB")
    assert sml["bucket"] == "beat_small"
    assert sml["grade"]["source"] == "none"  # no transcript -> neutral

    # Persisted: scan + per-ticker evidence + status record.
    assert store.data.get("e18:scan:latest") is not None
    assert store.data.get("e18:evidence:BIGB", {}).get("grade", {}).get("source") == "llm"
    assert store.data.get("e18:last_run", {}).get("ok") is True
    # Grade log captures both scores for the grader-vs-grader sample.
    log = store.data.get("e18:grades:log") or []
    assert any(e["ticker"] == "BIGB" and e["llmScore"] == 0.9 for e in log)


def test_build_scan_liquidity_floor():
    store = FakeStore()
    payload = pipeline.build_scan(
        flags=FLAGS,
        eodhd_client=FakeEodhd(thin_ticker="BIGB"),
        transcript_provider=FakeTranscripts(),
        llm_fn=_llm,
        store=store,
        as_of=AS_OF,
        universe=UNIVERSE,
    )
    tickers = [c["ticker"] for c in payload["candidates"]]
    assert "BIGB" not in tickers          # $50k ADV < $10M floor
    assert payload["meta"]["skippedLiquidity"] == 1


def test_build_scan_llm_fallback_path():
    store = FakeStore()
    payload = pipeline.build_scan(
        flags=FLAGS,
        eodhd_client=FakeEodhd(),
        transcript_provider=FakeTranscripts(),
        llm_fn=lambda text: None,  # LLM down
        store=store,
        as_of=AS_OF,
        universe=UNIVERSE,
    )
    big = next(c for c in payload["candidates"] if c["ticker"] == "BIGB")
    assert big["grade"]["source"] == "heuristic"
    assert big["grade"]["score"] == big["grade"]["heuristic_score"]


def test_rescore_from_store_no_network():
    store = FakeStore()
    pipeline.build_scan(
        flags=FLAGS, eodhd_client=FakeEodhd(), transcript_provider=FakeTranscripts(),
        llm_fn=_llm, store=store, as_of=AS_OF, universe=UNIVERSE,
    )
    out = pipeline.rescore_from_store(flags=FLAGS, store=store)
    assert out is not None
    assert {c["ticker"] for c in out["candidates"]} == {"BIGB", "SMLB"}
    assert out["meta"].get("rescoredAt")


def test_rescore_from_store_empty_returns_none():
    assert pipeline.rescore_from_store(flags=FLAGS, store=FakeStore()) is None
