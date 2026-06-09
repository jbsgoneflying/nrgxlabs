"""Tests for the Desk Brain meta-allocator.

Focus: the deterministic allocator (sizing, caps, clamped tilt) and the
aggregator's normalisation of engine signals into opportunities. The LLM
layer is intentionally untested here — it is bounded to a clamped tilt that
the allocator re-applies, so the allocator tests cover the safety contract.
"""
from __future__ import annotations

from backend.desk_brain import aggregator, allocator, sleeves
from backend.desk_brain.aggregator import Opportunity
from backend.desk_brain.allocator import RiskConfig


def _opp(engine_id, ticker, conviction, verdict="TRADABLE", desk_status="", sleeve=None):
    return Opportunity(
        engine_id=engine_id,
        engine_name=f"E{engine_id}",
        sleeve=sleeve or sleeves.sleeve_for_engine(engine_id),
        ticker=ticker,
        direction="long",
        structure="trend",
        conviction=conviction,
        verdict=verdict,
        desk_status=desk_status,
    )


# ---------------------------------------------------------------------------
# Sleeves / edge config
# ---------------------------------------------------------------------------


def test_sleeve_mapping_and_edges():
    assert sleeves.sleeve_for_engine(5) == sleeves.SLEEVE_DIRECTIONAL
    assert sleeves.sleeve_for_engine(2) == sleeves.SLEEVE_VOLATILITY
    assert sleeves.sleeve_for_engine(8) == sleeves.SLEEVE_OVERLAY

    # Ichimoku (E5) should out-edge Red Dog (E4) given the priors.
    e5 = sleeves.get_engine_edge(5)
    e4 = sleeves.get_engine_edge(4)
    assert 0.0 <= e4.edge_score <= 1.0
    assert 0.0 <= e5.edge_score <= 1.0
    assert e5.edge_score > e4.edge_score


def test_regime_weights_sum_to_one():
    for label in ("Risk-On", "Transitional", "Risk-Off", "Stressed", "unknown"):
        w = sleeves.regime_sleeve_weights(label)
        assert abs(sum(w.values()) - 1.0) < 1e-9


def test_stressed_regime_cuts_short_vol():
    risk_on = sleeves.regime_sleeve_weights("Risk-On")
    stressed = sleeves.regime_sleeve_weights("Stressed")
    assert stressed[sleeves.SLEEVE_VOLATILITY] < risk_on[sleeves.SLEEVE_VOLATILITY]
    assert stressed[sleeves.SLEEVE_OVERLAY] > risk_on[sleeves.SLEEVE_OVERLAY]


# ---------------------------------------------------------------------------
# Allocator — determinism + caps
# ---------------------------------------------------------------------------


def test_allocate_is_deterministic():
    opps = [_opp(5, "NVDA", 80), _opp(4, "AAPL", 70), _opp(5, "MSFT", 60)]
    b1 = allocator.allocate(opps, regime_label="Risk-On")
    b2 = allocator.allocate(opps, regime_label="Risk-On")
    assert b1.to_dict() == b2.to_dict()


def test_per_trade_risk_cap_respected():
    cfg = RiskConfig(per_trade_risk_pct=1.0, total_heat_pct=6.0)
    opps = [_opp(5, "NVDA", 95)]
    book = allocator.allocate(opps, regime_label="Risk-On", config=cfg)
    assert book.positions
    for p in book.positions:
        assert p.risk_pct <= cfg.per_trade_risk_pct + 1e-9


def test_total_concurrency_cap_respected():
    cfg = RiskConfig(max_concurrent_total=3, max_concurrent_per_sleeve=10)
    opps = [_opp(5, f"T{i}", 80 - i) for i in range(8)]
    book = allocator.allocate(opps, regime_label="Risk-On", config=cfg)
    assert len(book.positions) == 3
    assert book.caps["droppedForTotalCap"] == 5


def test_per_sleeve_concurrency_cap_respected():
    cfg = RiskConfig(max_concurrent_per_sleeve=2, max_concurrent_total=20)
    opps = [_opp(5, f"D{i}", 80 - i) for i in range(5)]
    book = allocator.allocate(opps, regime_label="Risk-On", config=cfg)
    directional = [p for p in book.positions if p.sleeve == sleeves.SLEEVE_DIRECTIONAL]
    assert len(directional) == 2


def test_total_heat_never_exceeds_budget():
    cfg = RiskConfig(total_heat_pct=6.0, per_trade_risk_pct=2.0)
    opps = [_opp(5, f"T{i}", 90) for i in range(10)] + [_opp(2, f"V{i}", 90) for i in range(5)]
    book = allocator.allocate(opps, regime_label="Risk-On", config=cfg)
    assert book.total_deployed_pct <= cfg.total_heat_pct + 1e-6
    assert abs(book.total_deployed_pct + book.reserve_pct - cfg.total_heat_pct) < 1e-6


def test_only_actionable_opportunities_sized():
    opps = [
        _opp(5, "NVDA", 80, verdict="STAND_DOWN"),     # not actionable
        _opp(5, "MSFT", 80, verdict="WATCH"),          # watch only, not live
        _opp(5, "AAPL", 80, verdict="WATCH", desk_status="entered"),  # live -> actionable
        _opp(5, "AMD", 80, verdict="TRADABLE"),        # actionable
    ]
    book = allocator.allocate(opps, regime_label="Risk-On")
    tickers = {p.ticker for p in book.positions}
    assert tickers == {"AAPL", "AMD"}


def test_empty_book_holds_full_reserve():
    book = allocator.allocate([], regime_label="Transitional")
    assert book.positions == []
    assert book.reserve_pct == book.total_heat_budget_pct
    assert any("flat" in n.lower() for n in book.notes)


def test_overlay_sleeve_is_not_traded():
    opps = [_opp(8, "HYG", 90, sleeve=sleeves.SLEEVE_OVERLAY)]
    book = allocator.allocate(opps, regime_label="Risk-On")
    assert book.positions == []  # overlay opportunities never get sized


def test_correlation_haircut_on_duplicate_ticker():
    opps = [_opp(5, "NVDA", 80), _opp(4, "NVDA", 80)]
    book = allocator.allocate(opps, regime_label="Risk-On")
    assert any("NVDA" in c for c in book.conflicts)
    assert all(p.haircut >= 0.25 for p in book.positions if p.ticker == "NVDA")


# ---------------------------------------------------------------------------
# Allocator — clamped LLM tilt (the safety contract)
# ---------------------------------------------------------------------------


def test_tilt_is_clamped():
    cfg = RiskConfig(tilt_max_pct=20.0)
    # LLM tries an absurd 5x tilt to directional and 0x to volatility.
    rogue = {"directional": 5.0, "volatility": 0.0, "overlay": 1.0}
    clamped = allocator.clamp_tilt(rogue, tilt_max_pct=cfg.tilt_max_pct)
    assert clamped["directional"] == 1.2
    assert clamped["volatility"] == 0.8
    assert clamped["overlay"] == 1.0


def test_rogue_tilt_cannot_blow_up_book():
    cfg = RiskConfig(total_heat_pct=6.0)
    opps = [_opp(5, f"T{i}", 80) for i in range(4)]
    rogue = {"directional": 99.0, "volatility": 0.0, "overlay": 0.0}
    book = allocator.allocate(opps, regime_label="Risk-On", config=cfg, sleeve_tilt=rogue)
    # Even with a rogue tilt, total heat stays within budget.
    assert book.total_deployed_pct <= cfg.total_heat_pct + 1e-6
    # Tilt recorded is the clamped value, not the rogue input.
    assert book.tilt_applied["directional"] == 1.2


def test_missing_tilt_defaults_neutral():
    clamped = allocator.clamp_tilt(None, tilt_max_pct=20.0)
    assert all(v == 1.0 for v in clamped.values())


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def _tracker_payload():
    return {
        "watching": [
            {"ticker": "NVDA", "direction": "long", "status": "watching", "signalDate": "2026-05-30",
             "quality": {"score": 82, "grade": "A+"},
             "verdict": {"status": "WATCH", "conviction": 70},
             "levels": {"riskDollars": 250, "reward1R": 2.0}},
        ],
        "entered": [
            {"ticker": "MSFT", "direction": "long", "status": "entered", "signalDate": "2026-05-29",
             "quality": {"score": 78, "grade": "A"},
             "verdict": {"status": "TRADABLE", "conviction": 80},
             "levels": {"riskDollars": 300}},
        ],
        "pending": [
            {"ticker": "AMD", "direction": "short", "status": "pending", "signalDate": "2026-05-31",
             "quality": {"score": 55, "grade": "B"},
             "verdict": {"status": "STAND_DOWN", "conviction": 40}},
        ],
    }


def test_aggregator_normalizes_tracker():
    opps = aggregator.from_ichimoku_tracker(_tracker_payload())
    by_ticker = {o.ticker: o for o in opps}
    assert set(by_ticker) == {"NVDA", "MSFT", "AMD"}
    assert by_ticker["NVDA"].engine_id == 5
    assert by_ticker["NVDA"].sleeve == sleeves.SLEEVE_DIRECTIONAL
    # Conviction prefers the reconciled verdict conviction.
    assert by_ticker["MSFT"].conviction == 80
    # Live desk states are actionable even if verdict is only WATCH.
    assert by_ticker["NVDA"].is_live and by_ticker["NVDA"].is_actionable
    # STAND_DOWN pending is not actionable.
    assert not by_ticker["AMD"].is_actionable


def test_aggregator_dedupes_across_buckets():
    payload = {
        "entered": [{"ticker": "NVDA", "status": "entered", "signalDate": "2026-05-30",
                     "quality": {"score": 80}, "verdict": {"status": "TRADABLE"}}],
        "pending": [{"ticker": "NVDA", "status": "pending", "signalDate": "2026-05-30",
                     "quality": {"score": 80}, "verdict": {"status": "STAND_DOWN"}}],
    }
    opps = aggregator.from_reddog_tracker(payload)
    assert len(opps) == 1
    assert opps[0].desk_status == "entered"  # desk state wins over raw scan bucket


def test_build_opportunity_set_combines_sources():
    opps = aggregator.build_opportunity_set(
        ichimoku_tracker=_tracker_payload(),
        reddog_tracker={"entered": [{"ticker": "TSLA", "status": "entered", "signalDate": "2026-05-30",
                                     "quality": {"score": 75}, "verdict": {"status": "TRADABLE"}}]},
    )
    engines = {o.engine_id for o in opps}
    assert engines == {4, 5}
    summary = aggregator.summarize_opportunities(opps)
    assert summary["total"] == len(opps)
    assert summary["actionable"] >= 1


# ---------------------------------------------------------------------------
# Earnings radar (facts) + aggregator wiring
# ---------------------------------------------------------------------------


import datetime as _dt

from backend.desk_brain import earnings_radar as _radar


def _fake_calendar(start, end):
    """Mimic eodhd_earnings_calendar.get_earnings_calendar output shape."""
    return {
        "2026-06-02": [
            {"ticker": "AVGO", "name": "Broadcom", "report_date": "2026-06-02",
             "timing_label": "AMC", "market_cap": 2_100_000_000_000.0, "sector": "Technology"},
        ],
        "2026-06-05": [
            {"ticker": "ORCL", "name": "Oracle", "report_date": "2026-06-05",
             "timing_label": "BMO", "market_cap": 400_000_000_000.0, "sector": "Technology"},
        ],
    }


def test_earnings_radar_facts_and_base_materiality(monkeypatch):
    monkeypatch.setattr(
        "backend.eodhd_earnings_calendar.get_earnings_calendar", _fake_calendar, raising=False
    )
    radar = _radar.get_earnings_radar(
        days_ahead=7, with_llm=False, as_of=_dt.date(2026, 6, 1)
    )
    assert radar["count"] == 2
    by_ticker = {r["ticker"]: r for r in radar["reporters"]}
    # Facts preserved verbatim from the calendar source.
    assert by_ticker["AVGO"]["timing"] == "AMC"
    assert by_ticker["AVGO"]["reportDate"] == "2026-06-02"
    assert by_ticker["ORCL"]["timing"] == "BMO"
    # Base materiality is bounded and AVGO (bigger + sooner) outranks ORCL.
    for r in radar["reporters"]:
        assert 0.0 <= r["materiality"] <= 100.0
        assert r["llmMateriality"] is None  # LLM disabled
    assert by_ticker["AVGO"]["materiality"] > by_ticker["ORCL"]["materiality"]
    # Sorted by materiality descending.
    assert radar["reporters"][0]["ticker"] == "AVGO"
    assert radar["llmSource"] == "none"


def test_earnings_radar_handles_source_failure(monkeypatch):
    def _boom(start, end):
        raise RuntimeError("EODHD_API_TOKEN not set")

    monkeypatch.setattr(
        "backend.eodhd_earnings_calendar.get_earnings_calendar", _boom, raising=False
    )
    radar = _radar.get_earnings_radar(days_ahead=7, with_llm=False, as_of=_dt.date(2026, 6, 1))
    assert radar["count"] == 0
    assert radar["reporters"] == []


def test_aggregator_from_earnings_radar():
    radar = {
        "reporters": [
            {"ticker": "AVGO", "name": "Broadcom", "reportDate": "2026-06-02", "timing": "AMC",
             "marketCap": 2_100_000_000_000.0, "sector": "Technology", "daysToReport": 1, "materiality": 92.0},
            {"ticker": "ORCL", "name": "Oracle", "reportDate": "2026-06-05", "timing": "BMO",
             "marketCap": 400_000_000_000.0, "sector": "Technology", "daysToReport": 4, "materiality": 55.0},
        ]
    }
    opps = aggregator.from_earnings_radar(radar)
    assert len(opps) == 2
    avgo = next(o for o in opps if o.ticker == "AVGO")
    assert avgo.engine_id == 1
    assert avgo.sleeve == sleeves.SLEEVE_VOLATILITY
    assert avgo.structure == "earnings_ic"
    assert avgo.conviction == 92.0
    assert avgo.is_actionable
    assert "2026-06-02" in avgo.summary and "AMC" in avgo.summary
    # Empty / malformed input degrades safely.
    assert aggregator.from_earnings_radar(None) == []
    assert aggregator.from_earnings_radar({}) == []


def test_aggregator_from_engine18():
    scan = {
        "candidates": [
            {"ticker": "BIGB", "sizing": "full", "bucket": "beat_large",
             "grade": {"score": 0.9, "quintile": "Q5"},
             "report": {"surprise_pct": 0.30},
             "entry_date": "2026-06-09", "exit_date": "2026-06-23"},
            {"ticker": "SMLB", "sizing": "half", "bucket": "beat_small",
             "grade": {"score": 0.6, "quintile": "Q4"},
             "report": {"surprise_pct": 0.08},
             "entry_date": "2026-06-09", "exit_date": "2026-06-23"},
            # Pass-tier candidates never reach the allocator.
            {"ticker": "PASS", "sizing": "pass", "bucket": "beat_small",
             "grade": {"score": 0.2, "quintile": "Q1"},
             "report": {"surprise_pct": 0.06}},
        ]
    }
    opps = aggregator.from_engine18(scan)
    assert [o.ticker for o in opps] == ["BIGB", "SMLB"]
    big = opps[0]
    assert big.engine_id == 18
    assert big.sleeve == sleeves.SLEEVE_DIRECTIONAL
    assert big.direction == "long"           # engine is long-only by design
    assert big.structure == "earnings_drift"
    assert big.conviction > opps[1].conviction  # full+Q5 beats half+Q4
    assert big.is_actionable
    assert "Large beat" in big.summary and "Q5" in big.summary
    # Edge prior carries the validated bake-off sample.
    edge = sleeves.get_engine_edge(18)
    assert edge.sample == 843
    assert edge.sleeve == sleeves.SLEEVE_DIRECTIONAL
    assert edge.edge_score > sleeves.get_engine_edge(17).edge_score  # proven > unproven
    # Empty / malformed input degrades safely.
    assert aggregator.from_engine18(None) == []
    assert aggregator.from_engine18({}) == []


def test_aggregator_from_vix_alert():
    # No spike -> no opportunity.
    assert aggregator.from_vix_alert({"detected": False}) == []
    assert aggregator.from_vix_alert(None) == []
    # Spike detected -> one fade opportunity in the volatility sleeve.
    opps = aggregator.from_vix_alert(
        {"detected": True, "vixCurrent": 32.0, "spikePctAboveMA": 45.0, "zScore": 2.4}
    )
    assert len(opps) == 1
    o = opps[0]
    assert o.engine_id == 12
    assert o.ticker == "VIX"
    assert o.sleeve == sleeves.SLEEVE_VOLATILITY
    assert o.direction == "sell_vol"
    assert 40.0 <= o.conviction <= 100.0
    assert o.is_actionable
