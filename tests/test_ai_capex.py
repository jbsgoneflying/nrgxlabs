"""Tests for the AI Capex Reality Engine (Engine 17).

Focus: the *deterministic* layer — evidence model round-trips, the Reality
Score / Consensus Gap math, the six-label mapping, second-order propagation,
and trade-idea derivation. The LLM extractor + the Tier-2 web agent are
exercised only via their fallback / disabled paths (no network), since the
platform guardrail is that nothing the LLM emits drives a label or size
directly — the scorer does, from the evidence table.
"""
from __future__ import annotations

import time

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


def _multi_pos(n=6, *, ticker="NVDA", category="semis", mag=0.95, conf=0.9,
               timing=models.TIMING_NEAR, signal=models.SIG_CAPEX_UP):
    """Positive evidence spread across INDEPENDENT sources (issuer transcript,
    distinct news/web domains, fundamentals) so corroboration is satisfied."""
    srcs = [models.SOURCE_TRANSCRIPT, models.SOURCE_NEWS, models.SOURCE_WEB, models.SOURCE_FUNDAMENTAL]
    out = []
    for i in range(n):
        src = srcs[i % len(srcs)]
        url = (f"http://src{i}.example.com/a" if src in (models.SOURCE_NEWS, models.SOURCE_WEB) else "")
        out.append(CapexEvidence(
            ticker=ticker, category=category, source_type=src, signal_type=signal,
            claim=f"{signal} {i}", magnitude=mag, confidence=conf,
            timing=timing, polarity=1, source_url=url,
        ))
    return out


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
    evid = _multi_pos(6)  # corroborated across independent sources
    v = score.score_ticker("NVDA", "semis", evid, {"momentum6mPct": -10}, flags=FLAGS)
    assert v.reality_score >= FLAGS.AI_CAPEX_REALITY_REAL_MIN
    assert v.corroboration >= 2
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


def test_single_source_is_discounted_vs_corroborated():
    # Same item count + strength, but one collapses to a single issuer voice
    # (all transcript) -> lower reality + corroboration than the multi-source one.
    single = [_ev(models.SIG_CAPEX_UP, mag=0.95, conf=0.9) for _ in range(6)]
    multi = _multi_pos(6)
    vs = score.score_ticker("NVDA", "semis", single, {"momentum6mPct": -10}, flags=FLAGS)
    vm = score.score_ticker("NVDA", "semis", multi, {"momentum6mPct": -10}, flags=FLAGS)
    assert vs.corroboration == 1
    assert vm.corroboration >= 2
    assert vs.reality_score < vm.reality_score


def test_corroboration_surfaced_in_dict():
    v = score.score_ticker("NVDA", "semis", _multi_pos(4), {"momentum6mPct": -30}, flags=FLAGS)
    d = v.to_dict()
    assert "independentSources" in d and d["independentSources"] >= 2


def test_overhyped_by_positioning_without_hype():
    # Thin-but-real capex (reality < real_min) while the market is positioned
    # euphorically -> overhyped via the positioning>>reality gap, no hype tag.
    evid = [_ev(models.SIG_CAPEX_UP, mag=0.8, conf=0.7, timing=models.TIMING_NEAR) for _ in range(3)]
    ctx = {"momentum6mPct": 90, "pe": 120, "ratingDrift": 5, "ratingCount": 8}
    v = score.score_ticker("ARM", "semis", evid, ctx, flags=FLAGS)
    assert v.reality_score < FLAGS.AI_CAPEX_REALITY_REAL_MIN
    assert v.consensus_gap <= -FLAGS.AI_CAPEX_GAP_THRESHOLD
    assert v.label == models.LABEL_OVERHYPED
    assert v.direction == "short"
    assert v.hype_ratio < FLAGS.AI_CAPEX_HYPE_RATIO_MAX  # fired without any hype language


def test_delayed_when_delay_dominates():
    pos = _multi_pos(10, ticker="VST", category="power_infrastructure",
                     mag=0.9, conf=0.9, timing=models.TIMING_MID)
    dly = [CapexEvidence(ticker="VST", category="power_infrastructure", source_type=models.SOURCE_NEWS,
                         signal_type=models.SIG_DELAY, claim=f"delay {i}", magnitude=0.85, confidence=0.85,
                         timing=models.TIMING_NEAR, polarity=-1, source_url=f"http://d{i}.example.com")
           for i in range(6)]
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
    driver = _multi_pos(6, ticker="MSFT", category="cloud_providers")
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


def test_rate_limiter_blocks_until_budget_then_acquires(monkeypatch):
    """Blocking acquire must WAIT for a slot (not fall back) — the bug that
    silently degraded scans was acquire() giving up the instant the budget hit."""
    rl = extract._RateLimiter()
    monkeypatch.setattr(rl, "_max", lambda: 1)
    assert rl.acquire(block=True) is True          # first slot free
    assert rl.acquire(block=False) is False         # budget hit, non-blocking gives up
    # Pretend the first reservation is already ~old so a slot frees almost
    # immediately; blocking acquire should then succeed rather than fall back.
    rl._ts = [time.time() - 59.9]
    assert rl.acquire(block=True, timeout=5.0) is True


def test_rate_limiter_block_times_out(monkeypatch):
    rl = extract._RateLimiter()
    monkeypatch.setattr(rl, "_max", lambda: 1)
    assert rl.acquire(block=True) is True
    # Fresh reservation + tiny timeout -> can't get a slot in time -> False.
    assert rl.acquire(block=True, timeout=0.2) is False


def test_has_llm_grade_distinguishes_fallback_from_llm():
    fallback = [_ev(models.SIG_DEMAND_PULL, conf=0.35, source=models.SOURCE_NEWS)]
    fund_only = [_ev(models.SIG_CAPEX_UP, conf=0.9, source=models.SOURCE_FUNDAMENTAL)]
    llm = [_ev(models.SIG_CAPEX_UP, conf=0.8, source=models.SOURCE_TRANSCRIPT)]
    assert pipeline._has_llm_grade(fallback) is False
    assert pipeline._has_llm_grade(fund_only) is False   # fundamentals alone don't count
    assert pipeline._has_llm_grade(llm) is True


def test_build_scan_keeps_prior_llm_evidence_on_fallback_run(monkeypatch):
    """A fallback-grade run must not clobber a prior rich LLM trail."""
    from backend.ai_capex import ingest

    class _FakeStore:
        def __init__(self):
            self.saved = {}
            self.prior = {
                "NVDA": [_ev(models.SIG_CAPEX_UP, conf=0.85, source=models.SOURCE_TRANSCRIPT,
                             claim="Data-center revenue $75B, +92% YoY")],
            }

    fake = _FakeStore()
    monkeypatch.setattr(pipeline.store, "get_evidence",
                        lambda t, store=None: fake.prior.get(t, []))
    saved = {}
    monkeypatch.setattr(pipeline.store, "set_evidence",
                        lambda t, evid, ttl_s=0, store=None: saved.__setitem__(t, evid))
    monkeypatch.setattr(pipeline.store, "set_context", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.store, "set_scan", lambda *a, **k: None)
    # Only NVDA in the universe; this run yields shallow keyword-fallback evidence.
    monkeypatch.setattr(pipeline.models, "all_tickers", lambda: ["NVDA"])
    monkeypatch.setattr(pipeline.models, "ticker_to_category_map", lambda: {"NVDA": "semis"})
    monkeypatch.setattr(ingest, "gather_ticker_text", lambda *a, **k: {"transcripts": [], "news": [{"title": "x"}]})
    monkeypatch.setattr(ingest, "fetch_market_context", lambda t: {})
    monkeypatch.setattr(ingest, "fetch_capex_fundamental_evidence", lambda t, c: None)
    monkeypatch.setattr(pipeline.extract, "extract_evidence",
                        lambda t, c, b, model=None: [_ev(models.SIG_DEMAND_PULL, conf=0.3, source=models.SOURCE_NEWS)])

    pipeline.build_scan(flags=FLAGS, with_web_agent=False, store_obj=fake, persist=True)
    # The prior rich trail must have been kept (not overwritten by the fallback run).
    assert "NVDA" in saved
    assert pipeline._has_llm_grade(saved["NVDA"]) is True


class _StubFlags:
    AI_CAPEX_ENABLE_WEB_AGENT = False
    AI_CAPEX_WEB_AGENT_MODEL = "gpt-5.5"
    AI_CAPEX_MAX_WEB_CALLS = 12


def test_web_agent_skips_when_flag_off(monkeypatch):
    # When the flag is OFF it must return [] without touching the network.
    import backend.config as cfg
    monkeypatch.setattr(cfg, "get_flags", lambda: _StubFlags())
    monkeypatch.setattr(agent, "_get_openai_client",
                        lambda: (_ for _ in ()).throw(AssertionError("client must not be built when flag off")))
    assert agent.run_web_agent(["NVDA"]) == []


def test_web_agent_graceful_when_no_client(monkeypatch):
    # Flag ON but no usable client/Responses API -> empty, never raises.
    class _OnFlags(_StubFlags):
        AI_CAPEX_ENABLE_WEB_AGENT = True
    import backend.config as cfg
    monkeypatch.setattr(cfg, "get_flags", lambda: _OnFlags())
    monkeypatch.setattr(agent, "_get_openai_client", lambda: None)
    assert agent.run_web_agent(["NVDA"]) == []


# ---------------------------------------------------------------------------
# Pipeline (cheap path, no store)
# ---------------------------------------------------------------------------


def test_rescore_from_store_none_when_empty():
    # No Redis configured in tests -> no persisted evidence -> None.
    assert pipeline.rescore_from_store(flags=FLAGS, store_obj=None) is None


# ---------------------------------------------------------------------------
# Horizon / timeframe
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402
from backend.ai_capex import horizon as _horizon  # noqa: E402
from backend.ai_capex.models import TickerVerdict as _TV  # noqa: E402


def _actionable(label, *, direction="long", gap=30.0, conviction=55.0, near=0.6):
    v = _TV(ticker="NVDA", category="semis", label=label, direction=direction,
            conviction=conviction, consensus_gap=gap, reality_score=72.0)
    v.timing_mix = {"near": near, "mid": 1 - near, "far": 0.0}
    return v


def test_horizon_catalyst_anchored_for_consensus_gap():
    today = _dt.date(2026, 6, 1)
    v = _actionable(models.LABEL_CONSENSUS_NOT_UPDATED, gap=30.0)
    orats = {"nextErn": "2026-06-13", "nextErnTod": "amc", "iv30d": 0.45}
    h = _horizon.derive_horizon(v, orats, today=today)
    assert h["basis"] == "catalyst"
    assert h["daysToCatalyst"] == 12
    assert h["catalystDate"] == "2026-06-13"
    assert h["impliedMovePct"] > 0 and h["thesisMovePct"] > 0
    assert h["assessment"] in ("underpriced", "fair", "rich")


def test_horizon_underpriced_when_thesis_exceeds_implied():
    today = _dt.date(2026, 6, 1)
    v = _actionable(models.LABEL_CONSENSUS_NOT_UPDATED, gap=80.0)  # big mispricing -> big thesis move
    orats = {"nextErn": "2026-06-08", "iv30d": 0.20}              # low IV -> small implied move
    h = _horizon.derive_horizon(v, orats, today=today)
    assert h["assessment"] == "underpriced"


def test_horizon_structural_without_orats():
    v = _actionable(models.LABEL_REAL, near=0.7)
    h = _horizon.derive_horizon(v, {}, today=_dt.date(2026, 6, 1))
    assert h["basis"] == "structural"
    assert "catalystDate" not in h
    assert h["band"]


def test_horizon_delayed_is_date_uncertain():
    v = _TV(ticker="DUK", category="utilities", label=models.LABEL_DELAYED,
            direction="neutral", conviction=40.0)
    h = _horizon.derive_horizon(v, {}, today=_dt.date(2026, 6, 1))
    assert h["basis"] == "uncertain"
    assert "quarter" in h["band"].lower()


def test_horizon_empty_for_non_actionable():
    v = _TV(ticker="X", category="semis", label=models.LABEL_NEUTRAL, conviction=0.0)
    assert _horizon.derive_horizon(v, {}) == {}


def test_horizon_ignores_stale_earnings_date():
    today = _dt.date(2026, 6, 1)
    v = _actionable(models.LABEL_CONSENSUS_NOT_UPDATED)
    h = _horizon.derive_horizon(v, {"nextErn": "2026-05-01", "iv30d": 0.4}, today=today)
    assert "daysToCatalyst" not in h           # past date dropped
    assert h["basis"] == "structural"          # falls back to structural band


def test_fetch_orats_timing_parses_cores():
    class _Resp:
        rows = [{"ticker": "NVDA", "nextErn": "2026-08-14", "nextErnTod": "amc", "iv30d": 0.5, "ivRank": 42}]

    class _Client:
        def cores(self, *, ticker, fields):
            return _Resp()

    out = _horizon.fetch_orats_timing("NVDA", _Client())
    assert out["nextErn"] == "2026-08-14"
    assert out["iv30d"] == 0.5
    assert _horizon.fetch_orats_timing("NVDA", None) == {}


# ---------------------------------------------------------------------------
# Reported-capex fundamental evidence (the hard, independent cross-check)
# ---------------------------------------------------------------------------


def _fundamentals_with_capex(series):
    """Build an EODHD-shaped fundamentals dict from {date: capex} (capex as the
    raw negative cash outflow EODHD reports)."""
    quarterly = {
        d: {"date": d, "capitalExpenditures": str(v)} for d, v in series.items()
    }
    return {"Financials": {"Cash_Flow": {"quarterly": quarterly}}}


def test_capex_fundamental_evidence_rising():
    from backend.ai_capex import ingest
    # 5 quarters, latest (-3.0B) is +50% vs the year-ago quarter (-2.0B).
    fund = _fundamentals_with_capex({
        "2026-04-30": -3_000_000_000, "2026-01-31": -2_800_000_000,
        "2025-10-31": -2_500_000_000, "2025-07-31": -2_200_000_000,
        "2025-04-30": -2_000_000_000,
    })
    ev = ingest.capex_evidence_from_fundamentals("NVDA", "semis", fund)
    assert ev is not None
    assert ev.source_type == models.SOURCE_FUNDAMENTAL
    assert ev.signal_type == models.SIG_CAPEX_UP
    assert ev.polarity == 1
    assert ev.timing == models.TIMING_NEAR
    assert ev.is_hard_positive
    assert "+50%" in ev.claim and "$3.0B" in ev.claim


def test_capex_fundamental_evidence_falling():
    from backend.ai_capex import ingest
    fund = _fundamentals_with_capex({
        "2026-04-30": -1_000_000_000, "2026-01-31": -1_500_000_000,
        "2025-10-31": -1_800_000_000, "2025-07-31": -1_900_000_000,
        "2025-04-30": -2_000_000_000,
    })
    ev = ingest.capex_evidence_from_fundamentals("DELL", "hardware", fund)
    assert ev is not None
    assert ev.signal_type == models.SIG_CAPEX_DOWN
    assert ev.polarity == -1
    assert ev.is_hard_negative


def test_capex_fundamental_evidence_flat_or_missing_returns_none():
    from backend.ai_capex import ingest
    # < 8% YoY move -> no decisive signal.
    flat = _fundamentals_with_capex({
        "2026-04-30": -2_050_000_000, "2026-01-31": -2_000_000_000,
        "2025-10-31": -2_000_000_000, "2025-07-31": -2_000_000_000,
        "2025-04-30": -2_000_000_000,
    })
    assert ingest.capex_evidence_from_fundamentals("X", "semis", flat) is None
    # Too few periods to compute a YoY comparison.
    assert ingest.capex_evidence_from_fundamentals("X", "semis", _fundamentals_with_capex(
        {"2026-04-30": -3_000_000_000, "2026-01-31": -2_000_000_000})) is None
    # Unrecognised shape -> None, never raises.
    assert ingest.capex_evidence_from_fundamentals("X", "semis", {}) is None
    assert ingest.capex_evidence_from_fundamentals("X", "semis", {"Financials": "nope"}) is None


def test_fundamental_evidence_adds_independent_corroboration():
    """A dense single-call read (one issuer voice) is single-source; adding the
    audited reported-capex item gives it a second INDEPENDENT voice -> corroborated."""
    from backend.ai_capex import ingest
    call_only = [
        _ev(models.SIG_DEMAND_PULL, source=models.SOURCE_TRANSCRIPT, claim=f"demand {i}")
        for i in range(6)
    ]
    v_call = score.score_ticker("NVDA", "semis", call_only, {}, flags=FLAGS)
    assert v_call.corroboration == 1  # all one "issuer" voice

    fund = _fundamentals_with_capex({
        "2026-04-30": -3_000_000_000, "2026-01-31": -2_800_000_000,
        "2025-10-31": -2_500_000_000, "2025-07-31": -2_200_000_000,
        "2025-04-30": -2_000_000_000,
    })
    fund_ev = ingest.capex_evidence_from_fundamentals("NVDA", "semis", fund)
    v_both = score.score_ticker("NVDA", "semis", call_only + [fund_ev], {}, flags=FLAGS)
    assert v_both.corroboration == 2
    assert v_both.reality_score > v_call.reality_score  # corroboration lifts credit
