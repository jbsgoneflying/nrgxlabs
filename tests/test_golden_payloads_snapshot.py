import datetime as dt
import json
from pathlib import Path

import pytest

from backend.earnings_logic import compute_breach_stats
from tests.replay_orats_client import ReplayOratsClient


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "golden"

_ADDITIVE_KEYS = {
    # Live, informational-only overlays added after golden fixtures were generated.
    # These should not break deterministic golden comparisons for core Engine 1 payloads.
    "marketDealerGamma",
    "tickerDealerGamma",
    "goNoGo",
    "technicals",
    "expectedMove",
    "strikeTargets",
    "earningsHoldRisk",  # Hold risk extension (informational only)
}


@pytest.mark.parametrize("ticker", ["MU", "NKE"])
def test_golden_payload_flags_off_snapshot(ticker, monkeypatch):
    payload_path = FIXTURES_DIR / f"{ticker}.payload.json"
    tape_path = FIXTURES_DIR / f"{ticker}.tape.json"
    if not payload_path.exists() or not tape_path.exists():
        pytest.skip(
            f"Missing golden fixtures for {ticker}. Run `python3 scripts/generate_golden_payloads.py --tickers {ticker}` with ORATS_TOKEN set."
        )

    # Match the generator script's pinned environment (flags OFF).
    monkeypatch.setenv("STRICT_REALIZED_WINDOW", "false")
    monkeypatch.setenv("USE_BETA_POSTERIOR_FOR_DECISIONING", "false")
    monkeypatch.setenv("USE_BETA_CI_FOR_CONFIDENCE", "false")
    monkeypatch.setenv("BETA_PRIOR_ALPHA", "1.0")
    monkeypatch.setenv("BETA_PRIOR_BETA", "1.0")
    monkeypatch.setenv("ADD_K_CONSISTENT_OVERSHOOT", "false")
    monkeypatch.setenv("TRADEBUILDER_ENFORCE_OTM", "false")
    monkeypatch.setenv("ADD_EVENT_SHIFT_TELEMETRY", "true")

    expected = json.loads(payload_path.read_text(encoding="utf-8"))
    tape = json.loads(tape_path.read_text(encoding="utf-8"))
    client = ReplayOratsClient(tape)
    today = dt.date.fromisoformat("2025-03-01")

    out = compute_breach_stats(client=client, ticker=ticker, n=20, years=5, k=1.0, today=today)
    for k in list(_ADDITIVE_KEYS):
        out.pop(k, None)
    assert out == expected


