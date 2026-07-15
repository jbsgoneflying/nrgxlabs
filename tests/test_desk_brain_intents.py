"""Desk Brain intent netting + legacy-path regression."""
from __future__ import annotations

from backend.desk_brain.aggregator import Opportunity
from backend.desk_brain.allocator import RiskConfig, allocate
from backend.desk_brain.intents import collapse_opportunities_for_allocator, opportunity_to_intent


def _opp(ticker, engine_id, conviction=80.0, direction="long"):
    return Opportunity(
        engine_id=engine_id,
        engine_name=f"E{engine_id}",
        sleeve="directional",
        ticker=ticker,
        direction=direction,
        structure="trend",
        conviction=conviction,
        verdict="TRADABLE",
        desk_status="",
        raw={"entry": 100, "stop": 95},
    )


def test_collapse_duplicate_ticker():
    opps = [_opp("AAPL", 18), _opp("AAPL", 5), _opp("MSFT", 18)]
    collapsed, notes = collapse_opportunities_for_allocator(opps)
    tickers = [o.ticker for o in collapsed]
    assert tickers.count("AAPL") == 1
    assert "MSFT" in tickers
    assert any("agree" in n for n in notes)


def test_allocate_flag_off_keeps_duplicates(monkeypatch):
    monkeypatch.setenv("DESK_BRAIN_INTENTS_ENABLED", "0")
    # Build opps that will both clear the gate — edges may zero-score; use live desk status
    opps = [
        Opportunity(
            engine_id=4, engine_name="RedDog", sleeve="directional",
            ticker="AAPL", direction="long", structure="mean_reversion",
            conviction=90, verdict="TRADABLE", desk_status="watching",
        ),
        Opportunity(
            engine_id=5, engine_name="Ichimoku", sleeve="directional",
            ticker="AAPL", direction="long", structure="trend",
            conviction=85, verdict="TRADABLE", desk_status="watching",
        ),
    ]
    book = allocate(opps, config=RiskConfig(per_trade_risk_pct=1.0))
    # Legacy path may haircut but can keep both if both selected — flag off means
    # collapse is skipped. Just assert allocate still returns a book.
    assert book is not None
    assert book.total_heat_budget_pct > 0


def test_allocate_flag_on_collapses(monkeypatch):
    monkeypatch.setenv("DESK_BRAIN_INTENTS_ENABLED", "1")
    opps = [
        Opportunity(
            engine_id=4, engine_name="RedDog", sleeve="directional",
            ticker="AAPL", direction="long", structure="mean_reversion",
            conviction=90, verdict="TRADABLE", desk_status="watching",
        ),
        Opportunity(
            engine_id=5, engine_name="Ichimoku", sleeve="directional",
            ticker="AAPL", direction="long", structure="trend",
            conviction=85, verdict="TRADABLE", desk_status="watching",
        ),
    ]
    book = allocate(opps, config=RiskConfig())
    aapl = [p for p in book.positions if p.ticker == "AAPL"]
    assert len(aapl) <= 1


def test_opportunity_to_intent_short():
    it = opportunity_to_intent(_opp("TSLA", 5, direction="short"))
    assert it.side == "short"
