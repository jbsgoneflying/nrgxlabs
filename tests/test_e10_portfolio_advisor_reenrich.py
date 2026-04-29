"""Engine 10 — /api/breach-compare/advisor fast-path re-enrichment.

Regression for the bug where the public ``/api/breach-compare`` endpoint
strips ``deskConsensus`` from the response when ``E1_EMIT_DESK_CONSENSUS=False``
(the v2 default). The frontend then re-posts those stripped rankings to
``/api/breach-compare/advisor``; the deterministic allocator inside Engine 10
keys off ``deskConsensus.verdict``, so every ticker silently defaults to
``PASS`` and the LLM dutifully reports "no candidates, stand down".

The fix: ``_ensure_desk_consensus_on_rankings`` recomputes the verdict from
``vrpAnalysis`` / ``entryQuality`` / ``emBreachSummary`` (+ regime / macro
context when present) before we hand the rankings to the allocator.
"""
from __future__ import annotations

from backend.routers.engine1_breach import _ensure_desk_consensus_on_rankings
from backend.e10_portfolio_advisor import compute_portfolio_allocation


def _ranking(ticker: str, vrp_score: float, eq_score: float, breach_summary: dict) -> dict:
    """Mimic the slim per-ticker payload the client sends (deskConsensus stripped)."""
    return {
        "ticker": ticker,
        "compositeScore": 75.0,
        "rank": 1,
        "fullPayload": {
            "vrpAnalysis": {
                "vrpScore": vrp_score,
                "ivElevation": 1.10,
                "meanRatio": 0.65,
                "confidence": "HIGH",
            },
            "entryQuality": {"entryQuality": eq_score, "flags": []},
            "emBreachSummary": breach_summary,
            "current": {"stockPrice": 200.0, "impliedMovePct": 4.5},
            "summary": {"breachRate": 12.0},
            "widthComparison": [
                {"emMult": 1.5, "wingWidthPts": 5.0, "breachPct": breach_summary.get("1.5", 0),
                 "creditProxy": 80.0, "maxLoss": 420.0},
            ],
            "nextEvent": {"earnDateNext": "2026-04-30", "timingPlanned": "AMC"},
        },
    }


def test_reenrich_recovers_lean_pass_from_stripped_rankings():
    """Stripped rankings (no deskConsensus) get verdicts reattached and
    survive the deterministic allocator's tradeable filter."""
    rankings = [
        _ranking("GOOGL", vrp_score=66.9, eq_score=51.2, breach_summary={"1.0": 10.5, "1.5": 0.0, "2.0": 0.0}),
        _ranking("MSFT",  vrp_score=64.0, eq_score=51.2, breach_summary={"1.0": 26.3, "1.5": 0.0, "2.0": 0.0}),
        _ranking("AMZN",  vrp_score=66.3, eq_score=46.8, breach_summary={"1.0": 15.8, "1.5": 0.0, "2.0": 0.0}),
    ]

    for r in rankings:
        assert r["fullPayload"].get("deskConsensus") is None

    _ensure_desk_consensus_on_rankings(rankings)

    for r in rankings:
        dc = r["fullPayload"].get("deskConsensus")
        assert isinstance(dc, dict), f"{r['ticker']} missing deskConsensus after reenrich"
        assert dc.get("verdict") in ("TRADE", "LEAN_PASS"), (
            f"{r['ticker']} unexpectedly PASS: {dc}"
        )
        assert isinstance(r["fullPayload"].get("emPreference"), dict)


def test_allocator_deploys_capital_after_reenrich():
    """End-to-end: the screenshot bug — three solidly tradeable names returning
    0% deployed — must produce a non-empty allocation once we recompute the
    verdicts on the server side."""
    rankings = [
        _ranking("GOOGL", vrp_score=66.9, eq_score=51.2, breach_summary={"1.0": 10.5, "1.5": 0.0, "2.0": 0.0}),
        _ranking("MSFT",  vrp_score=64.0, eq_score=51.2, breach_summary={"1.0": 26.3, "1.5": 0.0, "2.0": 0.0}),
        _ranking("AMZN",  vrp_score=66.3, eq_score=46.8, breach_summary={"1.0": 15.8, "1.5": 0.0, "2.0": 0.0}),
    ]

    pre = compute_portfolio_allocation(rankings, market_regime_label="moderate")
    assert pre["allocationCount"] == 0
    assert pre["allocations"] == []

    _ensure_desk_consensus_on_rankings(rankings)
    post = compute_portfolio_allocation(rankings, market_regime_label="moderate")

    assert post["allocationCount"] >= 1, post
    assert post["totalDeployed"] > 0, post
    assert post["cashReserve"] < 100, post
    deployed_tickers = {a["ticker"] for a in post["allocations"]}
    assert deployed_tickers <= {"GOOGL", "MSFT", "AMZN"}
    assert deployed_tickers, "expected at least one of GOOGL/MSFT/AMZN to deploy"


def test_reenrich_is_noop_when_consensus_already_present():
    """Slow-path rankings already carry deskConsensus — don't clobber them."""
    rankings = [{
        "ticker": "AAPL",
        "fullPayload": {
            "deskConsensus": {"verdict": "TRADE", "preferredEm": 1.5, "vrpScore": 80,
                               "entryQuality": 70, "bestBreachPct": 8.0, "reasons": []},
            "vrpAnalysis": {"vrpScore": 80},
            "entryQuality": {"entryQuality": 70},
            "emBreachSummary": {"1.0": 5.0, "1.5": 8.0, "2.0": 12.0},
            "emPreference": {"preferredEm": 1.5, "label": "standard"},
        },
    }]
    _ensure_desk_consensus_on_rankings(rankings)
    assert rankings[0]["fullPayload"]["deskConsensus"]["verdict"] == "TRADE"


def test_reenrich_skips_rankings_without_signal():
    """If a ranking has neither VRP nor breach data, we leave it alone instead
    of synthesizing a bogus verdict."""
    rankings = [{"ticker": "ZZZZ", "fullPayload": {}}]
    _ensure_desk_consensus_on_rankings(rankings)
    assert rankings[0]["fullPayload"].get("deskConsensus") in (None, {})
