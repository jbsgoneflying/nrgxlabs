"""Tests for the AI Capex Reality Engine (Engine 17).

Focus: the *deterministic* layer — evidence model round-trips, the Reality
Score / Consensus Gap math, the six-label mapping, second-order propagation,
and trade-idea derivation. The LLM extractor + the Tier-2 web agent are
exercised only via their fallback / disabled paths (no network), since the
platform guardrail is that nothing the LLM emits drives a label or size
directly — the scorer does, from the evidence table.
"""
from __future__ import annotations

from backend.ai_capex import agent, extract, models, pipeline, score, trades
from backend.ai_capex.models import CapexEvidence
from backend.config import get_flags

FLAGS = get_flags()


def _ev(signal_type, *, mag=0.9, conf=0.9, timing=models.TIMING_NEAR,
        polarity=1, source=models.SOURCE_TRANSCRIPT, ticker="NVDA", category="semis", claim=None):
    return CapexEvidence(
        ticker=ticker, category=category, source_type=source, signal_type=signal_type,
        claim=claim or f"{signal_type} observation", magnitude=mag, confidence=conf,
        timing=timing, polarity=polarity,
    )


# ---------------------------------------------------------------------------
# Models + taxonomy
# ---------------------------------------------------------------------------


def test_evidence_id_stable_and_roundtrip():
    e1 = _ev(models.SIG_CAPEX_UP, claim="Raising 2026 capex to $80B")
    e2 = _ev(models.SIG_CAPEX_UP, claim="Raising 2026 capex to $80B")
    assert e1.evidence_id == e2.evidence_id  # deterministic id
    rt = CapexEvidence.from_dict(e1.to_dict())
    assert rt.evidence_id == e1.evidence_id
    assert rt.signal_type == e1.signal_type
    assert rt.magnitude == e1.magnitude


def test_evidence_validation_clamps():
    e = _ev(models.SIG_CAPEX_UP, mag=5.0, conf=-1.0, timing="bogus", polarity=7)
    assert 0.0 <= e.magnitude <= 1.0
    assert 0.0 <= e.confidence <= 1.0
    assert e.timing in models.VALID_TIMINGS
    assert e.polarity in (-1, 0, 1)


def test_universe_loads():
    cats = models.load_universe().get("categories", {})
    assert "semis" in cats and "cloud_providers" in cats
    assert models.category_of("NVDA") == "semis"
    assert "NVDA" in models.all_tickers()
    assert models.category_role("cloud_providers") == "driver"


# ---------------------------------------------------------------------------
# Market positioning + gap
# ---------------------------------------------------------------------------


def test_market_positioning_neutral_when_empty():
    assert score.market_positioning_score({}) == 50.0


def test_market_positioning_bullish_vs_bearish():
    bull = score.market_positioning_score({"momentum6mPct": 60, "pe": 90, "ratingDrift": 4, "ratingCount": 6})
    bear = score.market_positioning_score({"momentum6mPct": -40, "pe": 8, "ratingDrift": -3, "ratingCount": 6})
    assert bull > 70 and bear < 35


# ---------------------------------------------------------------------------
# Single-ticker label mapping
# ---------------------------------------------------------------------------


def test_neutral_when_no_evidence():
    v = score.score_ticker("NVDA", "semis", [], {}, flags=FLAGS)
    assert v.label == models.LABEL_NEUTRAL
    assert not v.is_actionable


def test_real_beneficiary_when_priced():
    evid = [_ev(models.SIG_CAPEX_UP, mag=0.95, conf=0.9) for _ in range(5)]
    # Mildly negative momentum => positioning ~ low-40s => gap below threshold.
    v = score.score_ticker("NVDA", "semis", evid, {"momentum6mPct": -10}, flags=FLAGS)
    assert v.reality_score >= FLAGS.AI_CAPEX_REALITY_REAL_MIN
    assert v.label in (models.LABEL_REAL, models.LABEL_CONSENSUS_NOT_UPDATED)
    assert v.direction == "long"
    assert v.is_actionable


def test_consensus_not_updated_when_unpriced():
    evid = [_ev(models.SIG_CAPEX_UP, mag=0.95, conf=0.9) for _ in range(5)]
    v = score.score_ticker("NVDA", "semis", evid, {"momentum6mPct": -45}, flags=FLAGS)
    assert v.label == models.LABEL_CONSENSUS_NOT_UPDATED
    assert v.consensus_gap >= FLAGS.AI_CAPEX_GAP_THRESHOLD
    assert v.direction == "long"


def test_overhyped_when_hype_and_priced():
    evid = [_ev(models.SIG_HYPE, mag=0.5, conf=0.6, timing=models.TIMING_MID,
                polarity=0, source=models.SOURCE_NEWS, ticker="AI", category="ai_software")
            for _ in range(5)]
    ctx = {"momentum6mPct": 60, "pe": 90, "ratingDrift": 4, "ratingCount": 6}
    v = score.score_ticker("AI", "ai_software", evid, ctx, flags=FLAGS)
    assert v.label == models.LABEL_OVERHYPED
    assert v.direction == "short"
    assert v.hype_ratio >= FLAGS.AI_CAPEX_HYPE_RATIO_MAX


def test_delayed_when_delay_dominates():
    pos = [_ev(models.SIG_CAPEX_UP, mag=0.9, conf=0.9, timing=models.TIMING_MID) for _ in range(6)]
    dly = [_ev(models.SIG_DELAY, mag=0.8, conf=0.8, timing=models.TIMING_NEAR,
               polarity=-1, source=models.SOURCE_NEWS) for _ in range(4)]
    v = score.score_ticker("VST", "power_infrastructure", pos + dly, {}, flags=FLAGS)
    assert v.label == models.LABEL_DELAYED
    assert v.direction == "neutral"


def test_scoring_is_deterministic():
    evid = [_ev(models.SIG_CAPEX_UP, mag=0.95, conf=0.9) for _ in range(5)]
    ctx = {"momentum6mPct": -45}
    a = score.score_ticker("NVDA", "semis", evid, ctx, flags=FLAGS).to_dict()
    b = score.score_ticker("NVDA", "semis", evid, ctx, flags=FLAGS).to_dict()
    assert a == b


# ---------------------------------------------------------------------------
# Universe scoring + second-order propagation
# ---------------------------------------------------------------------------


def test_second_order_winner_propagation():
    # Strong, corroborated real capex at a DRIVER (cloud) name...
    driver = [_ev(models.SIG_CAPEX_UP, mag=0.95, conf=0.9, ticker="MSFT", category="cloud_providers")
              for _ in range(6)]
    evidence_by_ticker = {
        "MSFT": driver,
        "VRT": [],   # second-order name with no own evidence yet
    }
    verdicts = score.score_universe(evidence_by_ticker, {}, flags=FLAGS)
    by_t = {v.ticker: v for v in verdicts}
    assert by_t["MSFT"].reality_score >= FLAGS.AI_CAPEX_REALITY_REAL_MIN
    # VRT (electrical/cooling) should be lifted to a second-order winner.
    assert by_t["VRT"].label == models.LABEL_SECOND_ORDER_WINNER
    assert by_t["VRT"].direction == "long"
    assert by_t["VRT"].conviction > 0


def test_universe_ordering_actionable_first():
    evidence_by_ticker = {
        "NVDA": [_ev(models.SIG_CAPEX_UP, mag=0.95, conf=0.9) for _ in range(5)],
        "MU": [],
    }
    verdicts = score.score_universe(evidence_by_ticker, {"NVDA": {"momentum6mPct": -45}}, flags=FLAGS)
    # First verdict should be actionable (NVDA), neutral MU last.
    assert verdicts[0].is_actionable
    assert verdicts[-1].ticker == "MU"


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------


def test_trade_ideas_for_long_verdict():
    v = score.score_ticker("NVDA", "semis",
                            [_ev(models.SIG_CAPEX_UP, mag=0.95, conf=0.9) for _ in range(5)],
                            {"momentum6mPct": -45}, flags=FLAGS)
    ideas = trades.build_trade_ideas(v, orats_client=None)
    types = {i["type"] for i in ideas}
    assert "directional" in types and "options" in types


def test_baskets_group_same_direction():
    longs = []
    for t in ("NVDA", "AMD", "AVGO"):
        longs.append(score.score_ticker(
            t, "semis", [_ev(models.SIG_CAPEX_UP, mag=0.95, conf=0.9, ticker=t) for _ in range(5)],
            {"momentum6mPct": -45}, flags=FLAGS,
        ))
    baskets = trades.build_baskets(longs)
    assert any(b["category"] == "semis" and b["direction"] == "long" and len(b["tickers"]) >= 2
               for b in baskets)


# ---------------------------------------------------------------------------
# Extractor fallback + agent gating (no network)
# ---------------------------------------------------------------------------


def test_extract_keyword_fallback(monkeypatch):
    monkeypatch.setattr(extract, "_get_openai_client", lambda: None)
    bundle = {
        "ticker": "VRT",
        "transcripts": [],
        "news": [
            {"date": "2026-05-01", "title": "Vertiv flags extended transformer lead times",
             "text": "Lead times for switchgear continue to extend amid data-center demand.",
             "url": "http://x", "source": "benzinga"},
        ],
    }
    evid = extract.extract_evidence("VRT", "electrical_equipment", bundle, model="gpt-5.5")
    assert len(evid) >= 1
    assert any(e.signal_type == models.SIG_SUPPLY_CONSTRAINT for e in evid)


def test_extract_empty_bundle_returns_empty():
    assert extract.extract_evidence("NVDA", "semis", {"transcripts": [], "news": []}) == []


def test_web_agent_disabled_by_default(monkeypatch):
    # Default flag is OFF -> returns [] without any network call.
    monkeypatch.setattr(agent, "_get_openai_client", lambda: (_ for _ in ()).throw(AssertionError("should not be called")))
    assert agent.run_web_agent(["NVDA"]) == []


# ---------------------------------------------------------------------------
# Pipeline (cheap path, no store)
# ---------------------------------------------------------------------------


def test_rescore_from_store_none_when_empty():
    # No Redis configured in tests -> no persisted evidence -> None.
    assert pipeline.rescore_from_store(flags=FLAGS, store_obj=None) is None
