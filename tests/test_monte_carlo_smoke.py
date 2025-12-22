import datetime as dt
import json
from pathlib import Path

import pytest

from backend.earnings_logic import compute_breach_stats
from tests.replay_orats_client import ReplayOratsClient


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "golden"


# Fill these after first run (see assertion message if missing).
EXPECTED = {
    "MU": {"seed": 6154368175708811480, "breachProbEither": 0.000000, "cvar95Total": 0.0},
    "NKE": {"seed": 15873674609952260458, "breachProbEither": 0.418500, "cvar95Total": 5.0},
}


@pytest.mark.parametrize("ticker", ["MU", "NKE"])
def test_mc_smoke_is_deterministic_and_stable(ticker, monkeypatch):
    tape_path = FIXTURES_DIR / f"{ticker}.tape.json"
    if not tape_path.exists():
        pytest.skip(f"Missing tape fixture for {ticker}.")

    # Enable MC, keep defaults deterministic and bounded for CI.
    monkeypatch.setenv("ENABLE_MONTE_CARLO_EARNINGS", "true")
    monkeypatch.setenv("MC_N_SIMS", "2000")
    monkeypatch.setenv("MC_GLOBAL_SEED", "1337")
    monkeypatch.setenv("MC_MIN_POOL", "12")
    monkeypatch.setenv("MC_MIN_IMPLIED_MOVE_PCT", "0.5")
    monkeypatch.setenv("MC_ENABLE_CONDITION_ON_REGIME", "true")
    monkeypatch.setenv("MC_ENABLE_CONDITION_ON_QUARTER", "true")
    monkeypatch.setenv("MC_ENABLE_RECENCY_WEIGHTING", "false")
    monkeypatch.setenv("MC_ENABLE_WING_OPTIMIZATION", "false")
    monkeypatch.setenv("MC_ENABLE_TAS_STABILITY", "false")

    tape = json.loads(tape_path.read_text(encoding="utf-8"))
    client = ReplayOratsClient(tape)
    today = dt.date.fromisoformat("2025-03-01")

    # Provide a deterministic next-earnings anchor for tapes (delayed plans may not return forward nextErn in /cores).
    override = {"date": "2025-03-01", "timing": "AMC"}
    out1 = compute_breach_stats(client=client, ticker=ticker, n=20, years=5, k=1.0, today=today, next_event_override=override)
    out2 = compute_breach_stats(client=client, ticker=ticker, n=20, years=5, k=1.0, today=today, next_event_override=override)

    mc1 = out1.get("monteCarlo") or {}
    mc2 = out2.get("monteCarlo") or {}
    assert mc1 == mc2, "MC output must be deterministic across repeated runs with the same seed + tape."

    assert int(mc1.get("nSims") or 0) == 2000
    assert isinstance(out1.get("nextEvent"), dict)
    assert out1["nextEvent"].get("impliedMovePctPlanned") is not None

    seed = int(mc1.get("seed") or 0)
    breach_either = float((mc1.get("breachProb") or {}).get("either") or 0.0)
    cvar95_total = (mc1.get("cvar95") or {}).get("total")
    cvar95_total = None if cvar95_total is None else float(cvar95_total)

    # Basic sanity bounds.
    assert seed != 0
    assert 0.0 <= breach_either <= 1.0
    assert cvar95_total is None or cvar95_total >= 0.0

    exp = EXPECTED.get(ticker)
    if not exp:
        msg = (
            f"Populate EXPECTED[{ticker!r}] with: "
            f"{{'seed': {seed}, 'breachProbEither': {breach_either:.6f}, 'cvar95Total': {cvar95_total}}}"
        )
        raise AssertionError(msg)

    assert seed == int(exp["seed"])
    assert breach_either == pytest.approx(float(exp["breachProbEither"]), abs=1e-6)
    if exp.get("cvar95Total") is None:
        assert cvar95_total is None
    else:
        assert cvar95_total == pytest.approx(float(exp["cvar95Total"]), abs=1e-6)


