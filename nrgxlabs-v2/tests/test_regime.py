"""Tests for the Phase 1 module 3 regime encoder (feature-space MVP)."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest
from fastapi.testclient import TestClient

from v2_app.foundation.regime import (
    FEATURE_NAMES,
    REGIME_LABELS,
    RegimeIndex,
    build_index_from_dms_history,
    extract_market_state,
    is_skeleton_default,
    regime_label,
)


# ── DMS doc fixtures ───────────────────────────────────────


def _dms(
    *,
    date: str,
    state: str = "Transitional",
    score: float = 50.0,
    vol_level: float = 18.0,
    term: str = "flat",
    skew: str = "neutral",
    news: str = "low",
    gates: dict[str, str] | None = None,
    earnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "date": date,
        "regime": {"state": state, "score": score, "drivers": []},
        "vol_state": {"level": vol_level, "term_structure": term, "skew": skew},
        "news_risk": {"today": news, "week_ahead": []},
        "engine_gates": gates or {
            "earnings": "allowed",
            "red_dog": "allowed",
            "ichimoku": "allowed",
            "index_income": "allowed",
            "post_event_ext": "allowed",
        },
        "earnings_candidates": earnings or [],
    }


def _risk_on(date: str) -> dict[str, Any]:
    return _dms(
        date=date, state="Risk-On", score=20.0, vol_level=14.0,
        term="contango", skew="low", news="low",
        earnings=[{"ticker": "AMC", "score": 7.5}, {"ticker": "NVDA", "score": 8.2}],
    )


def _stressed(date: str) -> dict[str, Any]:
    gates = {
        "earnings": "suppressed",
        "red_dog": "allowed",
        "ichimoku": "suppressed",
        "index_income": "suppressed",
        "post_event_ext": "suppressed",
    }
    return _dms(
        date=date, state="Stressed", score=92.0, vol_level=38.0,
        term="backwardation", skew="elevated", news="high",
        gates=gates,
        earnings=[],
    )


def _risk_off(date: str) -> dict[str, Any]:
    return _dms(
        date=date, state="Risk-Off", score=72.0, vol_level=27.0,
        term="backwardation", skew="elevated", news="medium",
        gates={
            "earnings": "selective",
            "red_dog": "allowed",
            "ichimoku": "suppressed",
            "index_income": "reduced",
            "post_event_ext": "suppressed",
        },
        earnings=[{"ticker": "META", "score": 4.0}],
    )


def _transitional(date: str) -> dict[str, Any]:
    return _dms(
        date=date, state="Transitional", score=48.0, vol_level=18.5,
        term="flat", skew="neutral", news="low",
        earnings=[{"ticker": "MSFT", "score": 6.1}],
    )


# ── Feature extraction ────────────────────────────────────


def test_feature_names_match_extractor_keys() -> None:
    feats = extract_market_state(_risk_on("2026-04-01"))
    assert list(feats.keys()) == FEATURE_NAMES


def test_extract_market_state_handles_categoricals() -> None:
    feats = extract_market_state(_stressed("2026-03-15"))
    assert feats["regimeScore"] == 92.0
    assert feats["volLevel"] == 38.0
    assert feats["volTermStructure"] == -1.0   # backwardation
    assert feats["volSkew"] == 1.0             # elevated
    assert feats["newsRiskToday"] == 2.0       # high
    assert 0.0 <= feats["engineGatesOpen"] <= 1.0
    assert feats["engineGatesOpen"] < 0.4      # mostly suppressed
    assert feats["earningsCandidatesN"] == 0.0


def test_extract_market_state_returns_none_on_missing_paths() -> None:
    feats = extract_market_state({"date": "2026-04-04"})
    for v in feats.values():
        assert v is None


def test_extract_handles_unknown_categorical_string() -> None:
    doc = _dms(date="2026-04-05", term="cosmic_ballet")
    feats = extract_market_state(doc)
    assert feats["volTermStructure"] is None  # unknown maps to None


def test_extract_filters_nan_inf() -> None:
    doc = _dms(date="2026-04-06")
    doc["regime"]["score"] = float("nan")
    doc["vol_state"]["level"] = float("inf")
    feats = extract_market_state(doc)
    assert feats["regimeScore"] is None
    assert feats["volLevel"] is None


def test_engine_gates_score_monotone() -> None:
    all_open = extract_market_state(_dms(date="2026-04-07"))
    all_closed = extract_market_state(_stressed("2026-04-08"))
    assert all_open["engineGatesOpen"] > all_closed["engineGatesOpen"]


def test_earnings_top_score_picks_max() -> None:
    feats = extract_market_state(_risk_on("2026-04-09"))
    assert feats["earningsCandidatesN"] == 2.0
    assert feats["earningsTopScore"] == 8.2


def test_earnings_top_score_skips_garbage_entries() -> None:
    doc = _dms(
        date="2026-04-10",
        earnings=[
            {"ticker": "X", "score": "not a number"},
            {"ticker": "Y", "score": 5.5},
            "not even a dict",
            {"ticker": "Z"},  # no score
        ],
    )
    feats = extract_market_state(doc)
    assert feats["earningsCandidatesN"] == 4.0  # n counts list length
    assert feats["earningsTopScore"] == 5.5


def test_regime_label_with_alt_field() -> None:
    doc = {"date": "x", "regime": {"label": "Risk-On"}}
    assert regime_label(doc) == "Risk-On"


def test_regime_label_returns_none_on_unknown() -> None:
    assert regime_label({"date": "x", "regime": {"state": "Pizzaaaaa"}}) is None
    assert regime_label({"date": "x"}) is None


# ── Skeleton-default detector ──────────────────────────────


def _skeleton_doc(date: str = "2026-04-15") -> dict[str, Any]:
    """Mirror what v1's build_daily_market_state writes when fed nothing."""
    return {
        "date": date,
        "regime": {"state": "Transitional", "score": 50.0, "drivers": []},
        "vol_state": {"level": 25.0, "term_structure": "flat", "skew": "neutral"},
        "news_risk": {"today": "low", "week_ahead": []},
        "engine_gates": {
            "earnings": "selective", "red_dog": "allowed", "ichimoku": "selective",
            "index_income": "allowed", "post_event_ext": "selective",
        },
        "earnings_candidates": [],
    }


def test_skeleton_default_via_explicit_tag() -> None:
    doc = _risk_on("2026-04-15")
    doc["data_quality"] = {"skeleton_default": True}
    assert is_skeleton_default(doc) is True


def test_skeleton_default_via_fingerprint() -> None:
    doc = _skeleton_doc()
    assert is_skeleton_default(doc) is True


def test_skeleton_with_real_probs_not_filtered() -> None:
    """A neutral day with real HMM probs is NOT a skeleton."""
    doc = _skeleton_doc()
    doc["regime"]["probs"] = {"risk_on": 0.30, "transitional": 0.40, "stressed": 0.30}
    assert is_skeleton_default(doc) is False


def test_skeleton_with_high_confidence_not_filtered() -> None:
    doc = _skeleton_doc()
    doc["regime"]["confidence"] = 0.72
    assert is_skeleton_default(doc) is False


def test_real_risk_on_doc_not_skeleton() -> None:
    assert is_skeleton_default(_risk_on("2026-04-15")) is False


def test_real_stressed_doc_not_skeleton() -> None:
    assert is_skeleton_default(_stressed("2026-04-15")) is False


def test_extract_returns_all_none_for_skeleton() -> None:
    feats = extract_market_state(_skeleton_doc())
    for v in feats.values():
        assert v is None


def test_build_index_skips_skeletons_separately() -> None:
    # 18 real synthetic + 5 skeleton defaults
    docs = _synthetic_corpus() + [_skeleton_doc(f"2026-05-0{i}") for i in range(5)]
    idx, stats = build_index_from_dms_history(docs)
    assert idx.n_indexed == 18
    assert stats["skipped"]["skeleton_default"] == 5


# ── End-to-end build + search ─────────────────────────────


def _synthetic_corpus() -> list[dict[str, Any]]:
    docs = []
    for i in range(6):
        docs.append(_risk_on(f"2026-01-0{i+1}"))
    for i in range(5):
        docs.append(_transitional(f"2026-02-0{i+1}"))
    for i in range(4):
        docs.append(_risk_off(f"2026-03-0{i+1}"))
    for i in range(3):
        docs.append(_stressed(f"2026-04-0{i+1}"))
    return docs


def test_build_index_from_synthetic_corpus() -> None:
    docs = _synthetic_corpus()
    idx, stats = build_index_from_dms_history(docs)
    assert idx.n_indexed == len(docs) == 18
    assert stats["n_seen"] == 18
    assert stats["label_distribution"]["Risk-On"] == 6
    assert stats["label_distribution"]["Stressed"] == 3
    assert all(0.0 <= c <= 1.0 for c in stats["feature_coverage"].values())
    # Most features observed on every day; earningsTopScore can be null on
    # days with zero earnings candidates (the 3 Stressed days), so coverage
    # there will be 15/18.
    assert stats["feature_coverage"]["regimeScore"] == 1.0
    assert stats["feature_coverage"]["volLevel"] == 1.0
    assert stats["feature_coverage"]["earningsTopScore"] == round(15 / 18, 3)


def test_build_skips_missing_date() -> None:
    docs = _synthetic_corpus() + [{"regime": {"state": "Risk-On"}}]  # no date
    idx, stats = build_index_from_dms_history(docs)
    assert stats["skipped"]["no_date"] == 1
    assert idx.n_indexed == len(_synthetic_corpus())


def test_build_skips_all_missing_features() -> None:
    docs = _synthetic_corpus() + [{"date": "2026-05-01"}]  # no features
    idx, stats = build_index_from_dms_history(docs)
    assert stats["skipped"]["all_features_missing"] == 1


def test_search_top_k_groups_by_label() -> None:
    docs = _synthetic_corpus()
    idx, _ = build_index_from_dms_history(docs)

    # Query a Risk-On day → top-K should be heavily Risk-On.
    q = extract_market_state(_risk_on("2026-99-99"))
    nbrs = idx.search(q, k=6)
    labels = [n["label"] for n in nbrs]
    assert labels.count("Risk-On") >= 4

    # Query a Stressed day → top-K should pull Stressed first.
    q2 = extract_market_state(_stressed("2026-99-99"))
    nbrs2 = idx.search(q2, k=3)
    labels2 = [n["label"] for n in nbrs2]
    assert labels2.count("Stressed") >= 2


def test_search_respects_date_exclude() -> None:
    docs = _synthetic_corpus()
    idx, _ = build_index_from_dms_history(docs)
    q_doc = _risk_on("2026-01-01")
    q = extract_market_state(q_doc)
    nbrs = idx.search(q, k=5, date_exclude="2026-01-01")
    assert all(n["date"] != "2026-01-01" for n in nbrs)


def test_search_handles_empty_index() -> None:
    idx = RegimeIndex(standardizer=__import__("v2_app.foundation.vectorspace", fromlist=["Standardizer"]).Standardizer(),
                       feature_names=FEATURE_NAMES)
    nbrs = idx.search({n: 0.0 for n in FEATURE_NAMES}, k=5)
    assert nbrs == []


def test_encode_returns_cluster_prior_summing_to_one() -> None:
    docs = _synthetic_corpus()
    idx, _ = build_index_from_dms_history(docs)
    q = extract_market_state(_stressed("2026-99-99"))
    out = idx.encode(q)
    assert out["n_indexed"] == 18
    assert len(out["embedding"]) == len(out["mask"]) == len(FEATURE_NAMES)
    assert set(out["knn_label_distribution"].keys()) == set(REGIME_LABELS)
    total = sum(out["knn_label_distribution"].values())
    assert math.isclose(total, 1.0, abs_tol=1e-6)
    assert out["knn_label_distribution"]["Stressed"] >= 0.3


def test_index_round_trip_via_json() -> None:
    docs = _synthetic_corpus()
    idx, _ = build_index_from_dms_history(docs)
    blob = idx.to_json()
    restored = RegimeIndex.from_json(blob)
    assert restored is not None
    assert restored.n_indexed == idx.n_indexed
    assert restored.feature_names == idx.feature_names
    q = extract_market_state(_risk_on("2026-99-99"))
    assert idx.search(q, k=3) == restored.search(q, k=3)


# ── Endpoint contracts ─────────────────────────────────────


class FakeRedis:
    """Tiny in-memory Redis stub for endpoint contract tests."""

    def __init__(self, dms_docs: list[dict[str, Any]] | None = None) -> None:
        self.kv: dict[str, str] = {}
        if dms_docs:
            for d in dms_docs:
                self.kv[f"front_layer:dms:{d['date']}"] = json.dumps(d)

    def get(self, key: str) -> str | None:
        return self.kv.get(key)

    def set(self, key: str, value: str) -> None:
        self.kv[key] = value

    def zrevrange(self, key: str, start: int, stop: int) -> list[str]:
        return []

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        return []

    def scan_iter(self, match: str = "*", count: int = 100):
        # Strip the trailing wildcard for the tests.
        prefix = match.rstrip("*")
        for k in list(self.kv.keys()):
            if k.startswith(prefix):
                yield k


def _client_with_redis(monkeypatch: pytest.MonkeyPatch, fake: FakeRedis) -> TestClient:
    from v2_app import main as v2_main
    from v2_app.foundation import regime_store

    monkeypatch.setattr(regime_store, "_redis_client", lambda: fake)

    return TestClient(v2_main.app)


def test_features_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    client = _client_with_redis(monkeypatch, fake)
    r = client.get("/api/v2/regime/features")
    assert r.status_code == 200
    body = r.json()
    assert body["engine"] == "regime"
    assert body["feature_names"] == FEATURE_NAMES


def test_stats_endpoint_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    client = _client_with_redis(monkeypatch, fake)
    r = client.get("/api/v2/regime/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["n_indexed"] == 0


def test_build_then_stats_then_nearest(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis(dms_docs=_synthetic_corpus())
    client = _client_with_redis(monkeypatch, fake)

    r = client.post("/api/v2/regime/build", json={"max_days": 50})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["n_indexed"] == 18
    assert body["label_distribution"]["Risk-On"] == 6

    r = client.get("/api/v2/regime/stats")
    assert r.status_code == 200
    sb = r.json()
    assert sb["n_indexed"] == 18
    assert sb["feature_names"] == FEATURE_NAMES

    # Nearest with a Risk-On query.
    r = client.post(
        "/api/v2/regime/nearest",
        json={"dms": _risk_on("2026-99-99"), "k": 5},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["n_indexed"] == 18
    assert len(body["neighbors"]) == 5
    labels = [n["label"] for n in body["neighbors"]]
    assert labels.count("Risk-On") >= 3

    # Embed returns cluster prior + embedding.
    r = client.post(
        "/api/v2/regime/embed",
        json={"dms": _stressed("2026-99-99")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["label"] == "Stressed"
    assert "embedding" in body
    assert "knn_label_distribution" in body


def test_nearest_with_explicit_features_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis(dms_docs=_synthetic_corpus())
    client = _client_with_redis(monkeypatch, fake)
    client.post("/api/v2/regime/build", json={"max_days": 50})

    feats = {n: 0.0 for n in FEATURE_NAMES}
    feats["regimeScore"] = 90.0
    feats["volLevel"] = 35.0
    feats["volTermStructure"] = -1.0
    feats["volSkew"] = 1.0
    feats["newsRiskToday"] = 2.0
    feats["engineGatesOpen"] = 0.2
    feats["earningsCandidatesN"] = 0.0
    feats["earningsTopScore"] = 0.0

    r = client.post("/api/v2/regime/nearest", json={"features": feats, "k": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert len(body["neighbors"]) == 3


def test_nearest_returns_not_built_before_build(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    client = _client_with_redis(monkeypatch, fake)
    r = client.post(
        "/api/v2/regime/nearest",
        json={"dms": _risk_on("2026-99-99"), "k": 3},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "not_built"
    assert body["neighbors"] == []


def test_payload_requires_features_or_dms(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis(dms_docs=_synthetic_corpus())
    client = _client_with_redis(monkeypatch, fake)
    client.post("/api/v2/regime/build", json={"max_days": 50})
    r = client.post("/api/v2/regime/nearest", json={"k": 3})
    assert r.status_code == 422


def test_legacy_get_endpoints_return_phase1_status(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    client = _client_with_redis(monkeypatch, fake)
    r = client.get("/api/v2/regime/embed?date=2026-04-01")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "phase1_mvp_active"

    r = client.get("/api/v2/regime/nearest?date=2026-04-01&k=3")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "phase1_mvp_active"


def test_regime_flag_visible_in_version(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    client = _client_with_redis(monkeypatch, fake)
    r = client.get("/api/v2/version")
    assert r.status_code == 200
    body = r.json()
    flags = body["foundation"]
    assert flags["regime_encoder"] is True
