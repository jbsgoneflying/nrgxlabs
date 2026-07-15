"""One-shot EODHD entitlement probe for the Equity Repricing Lab.

Converts the plan's data-layer assumptions into facts BEFORE the PR-2 backfill
commits to a scope. Probes, per docs/plans/nrgx_equity_repricing_lab_
implementation_plan.md §28:

  1. Delisted symbol list      — /exchange-symbol-list/US?delisted=1
  2. Delisted-ticker EOD bars  — daily history for a known delisted name
  3. Splits                    — a known split name (NVDA 2024-06-10 10:1)
  4. Dividends                 — AAPL dividend history
  5. Earnings-calendar depth   — AAPL 2017 window (actual + estimate fields)
  6. Fundamentals shares data  — SharesStats / outstandingShares presence
  7. Intraday endpoint         — /intraday/{symbol} entitlement check

Writes a JSON artifact to {REPRICING_LAB_RUNS_DIR}/probe-eodhd-{date}.json and
prints a human summary. Read-only against the vendor; never prints the token.

Usage:
    python3 scripts/lab_probe_eodhd.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.research.env_loader import load_research_env  # noqa: E402

# Known-answer fixtures for the probe.
_DELISTED_SAMPLE = "ATVI.US"        # Activision Blizzard — acquired/delisted 2023-10
_SPLIT_SAMPLE = ("NVDA.US", "2024-06-10", 10.0)  # 10-for-1 split
_DIVIDEND_SAMPLE = "AAPL.US"
_EARNINGS_DEPTH_SAMPLE = ("AAPL.US", "2017-01-01", "2017-12-31")
_INTRADAY_SAMPLE = "AAPL.US"


def _check(fn):
    """Run one probe; capture ok/detail/error without aborting the rest."""
    try:
        detail = fn()
        return {"ok": True, **(detail or {})}
    except Exception as exc:  # noqa: BLE001 — probe must report, not crash
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    load_research_env()
    from backend.config import get_flags
    from backend.eodhd_client import EodhdClient

    client = EodhdClient.from_env()
    results: dict = {}

    def probe_delisted_list():
        resp = client.get_exchange_symbols("US", delisted=True)
        rows = resp.rows or []
        sample = [str(r.get("Code") or "") for r in rows[:5]]
        return {"delistedCount": len(rows), "sampleCodes": sample}

    def probe_delisted_bars():
        resp = client.get_eod(_DELISTED_SAMPLE, from_date="2023-01-01", to_date="2023-12-31")
        rows = resp.rows or []
        return {
            "symbol": _DELISTED_SAMPLE,
            "barCount": len(rows),
            "firstDate": str(rows[0].get("date")) if rows else None,
            "lastDate": str(rows[-1].get("date")) if rows else None,
        }

    def probe_splits():
        sym, split_date, expected_ratio = _SPLIT_SAMPLE
        resp = client.get_splits(sym, from_date="2024-01-01", to_date="2024-12-31")
        rows = resp.rows or []
        hit = next((r for r in rows if str(r.get("date") or "")[:10] == split_date), None)
        return {
            "symbol": sym,
            "rowCount": len(rows),
            "expectedSplitFound": hit is not None,
            "expectedDate": split_date,
            "expectedRatio": expected_ratio,
            "row": hit,
        }

    def probe_dividends():
        resp = client.get_dividends(_DIVIDEND_SAMPLE, from_date="2024-01-01", to_date="2024-12-31")
        rows = resp.rows or []
        fields = sorted(rows[0].keys()) if rows else []
        return {"symbol": _DIVIDEND_SAMPLE, "rowCount": len(rows), "fields": fields}

    def probe_earnings_depth():
        sym, lo, hi = _EARNINGS_DEPTH_SAMPLE
        resp = client.get_calendar_earnings(symbols=sym, from_date=lo, to_date=hi)
        rows = resp.rows or []
        with_actual = sum(1 for r in rows if r.get("actual") is not None)
        with_estimate = sum(1 for r in rows if r.get("estimate") is not None)
        with_timing = sum(1 for r in rows if r.get("before_after_market"))
        return {
            "symbol": sym,
            "window": [lo, hi],
            "rowCount": len(rows),
            "withActual": with_actual,
            "withEstimate": with_estimate,
            "withTiming": with_timing,
        }

    def probe_fundamentals_shares():
        data = client.get_fundamentals("AAPL.US")
        shares_stats = (data or {}).get("SharesStats") or {}
        outstanding = (data or {}).get("outstandingShares") or {}
        general = (data or {}).get("General") or {}
        return {
            "symbol": "AAPL.US",
            "hasSharesStats": bool(shares_stats),
            "sharesStatsKeys": sorted(shares_stats.keys())[:12],
            "hasOutstandingSharesHistory": bool(outstanding),
            "outstandingSharesKeys": sorted(outstanding.keys())[:4],
            "sector": general.get("Sector"),
            "industry": general.get("Industry"),
        }

    def probe_intraday():
        # Not a client method (deliberately — plan defers intraday); raw GET
        # via the client's low-level path to test entitlement only.
        from backend.eodhd_client import EODHD_BASE_URL, _http_get

        url = f"{EODHD_BASE_URL}/intraday/{_INTRADAY_SAMPLE}"
        status, _headers, body = _http_get(
            url,
            {"api_token": os.getenv("EODHD_API_TOKEN"), "fmt": "json", "interval": "5m"},
            30.0,
        )
        entitled = status == 200
        row_count = 0
        if entitled:
            try:
                data = json.loads(body.decode("utf-8") or "[]")
                row_count = len(data) if isinstance(data, list) else 0
            except Exception:
                row_count = -1
        return {"symbol": _INTRADAY_SAMPLE, "httpStatus": status, "entitled": entitled, "rowCount": row_count}

    results["delisted_symbol_list"] = _check(probe_delisted_list)
    results["delisted_ticker_bars"] = _check(probe_delisted_bars)
    results["splits"] = _check(probe_splits)
    results["dividends"] = _check(probe_dividends)
    results["earnings_calendar_2017_depth"] = _check(probe_earnings_depth)
    results["fundamentals_shares"] = _check(probe_fundamentals_shares)
    results["intraday_entitlement"] = _check(probe_intraday)

    flags = get_flags()
    runs_dir = str(getattr(flags, "REPRICING_LAB_RUNS_DIR", "data/lab_runs"))
    os.makedirs(runs_dir, exist_ok=True)
    today = dt.date.today().isoformat()
    artifact = {
        "probe": "eodhd-entitlement",
        "ranAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "results": results,
    }
    path = os.path.join(runs_dir, f"probe-eodhd-{today}.json")
    with open(path, "w") as fh:
        json.dump(artifact, fh, indent=2, sort_keys=True)

    print(f"[lab-probe] written {path}")
    all_ok = True
    for name, res in results.items():
        ok = bool(res.get("ok"))
        all_ok = all_ok and ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: "
              + json.dumps({k: v for k, v in res.items() if k != 'ok'}, default=str)[:220])
    print(f"[lab-probe] overall: {'all probes returned' if all_ok else 'SOME PROBES FAILED — review before PR 2 backfill'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
