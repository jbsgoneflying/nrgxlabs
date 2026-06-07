"""Tests for the Phase 1 module 4 agent committee.

We don't hit the real Anthropic API in CI — a fake ``ClaudeClient``
returns role-specific canned JSON so we can verify:

  - Every role gets called in the correct order
  - System prompts are role-correct (smoke check)
  - JSON parsing is resilient to model wrapping (markdown fences, prose)
  - The router assembles Foundation Brain context, persists the
    decision, and returns a clean response shape
  - Roles endpoint and dry-run endpoint behave
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from v2_app.agents.committee import (
    AgentRole,
    CommitteeRunner,
    SetupPayload,
    SYSTEM_PROMPTS,
    _safe_parse_json,
)


# ── JSON parser ───────────────────────────────────────────


def test_safe_parse_json_plain() -> None:
    parsed, err = _safe_parse_json('{"foo": 1, "bar": "baz"}')
    assert err is None
    assert parsed == {"foo": 1, "bar": "baz"}


def test_safe_parse_json_with_markdown_fences() -> None:
    raw = "```json\n" + '{"decision": "approve", "size_pct_of_bp": 1.5}' + "\n```"
    parsed, err = _safe_parse_json(raw)
    assert err is None
    assert parsed["decision"] == "approve"


def test_safe_parse_json_extracts_first_object_from_prose() -> None:
    raw = "Here's my verdict:\n\n" + '{"lean": "constructive", "confidence": 0.7}' + "\n\nThanks."
    parsed, err = _safe_parse_json(raw)
    assert err is None
    assert parsed["lean"] == "constructive"


def test_safe_parse_json_empty() -> None:
    parsed, err = _safe_parse_json("")
    assert parsed == {}
    assert err == "empty response"


def test_safe_parse_json_unrecoverable() -> None:
    parsed, err = _safe_parse_json("nothing useful here at all")
    assert parsed == {}
    assert err is not None


# ── Fake client ───────────────────────────────────────────


CANNED_VERDICTS: dict[AgentRole, dict[str, Any]] = {
    AgentRole.RESEARCHER: {
        "headline": "Closest precedent: NVDA 2024-08-30 IC, won at 50% target.",
        "key_analogues": ["NVDA 2024-08-30 win", "META 2024-04-25 win"],
        "regime_context": "Today's regime resembles August 2024.",
        "lean": "constructive",
        "confidence": 0.62,
    },
    AgentRole.QUANT: {
        "p_win_estimate": 0.71,
        "p_breach_estimate": 0.18,
        "p_breach_interval": [0.10, 0.27],
        "expected_value_bps": 65,
        "lean": "constructive",
        "confidence": 0.66,
        "rationale": "Edge survives the calibration band.",
    },
    AgentRole.DEVILS_ADVOCATE: {
        "failure_mode": "earnings analogue set is heavily NVDA-concentrated.",
        "evidence": "5 of 5 nearest neighbors are large-cap mega-tech.",
        "p_failure_estimate": 0.32,
        "lean": "cautious",
        "confidence": 0.55,
    },
    AgentRole.RISK_OFFICER: {
        "max_size_pct_of_buying_power": 1.25,
        "stop_rule": "Exit at 2x credit received or 21 DTE.",
        "scale_in": False,
        "lean": "constructive",
        "confidence": 0.6,
        "rationale": "Standard E1 sizing, mid-confidence.",
    },
    AgentRole.SYNTHESIZER: {
        "decision": "approve",
        "headline": "Approve at 1.25%, 2x credit stop, 21 DTE rail.",
        "size_pct_of_bp": 1.25,
        "stop_rule": "Exit at 2x credit received or 21 DTE.",
        "key_dissent": "Devil's Advocate flags ticker concentration.",
        "calibration_warning": False,
        "confidence": 0.62,
    },
}


class FakeClaudeClient:
    """Returns canned role-tagged JSON. Records (role, model) per call."""

    def __init__(self, *, fail_role: AgentRole | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail_role = fail_role

    def complete(self, *, system: str, prompt: str, model: str) -> str:
        # Identify role by matching the system prompt to our table.
        role = None
        for r, sp in SYSTEM_PROMPTS.items():
            if sp == system:
                role = r
                break
        assert role is not None, "Unknown system prompt routed to fake client"
        self.calls.append((role.value, model))
        if role == self._fail_role:
            raise RuntimeError("simulated network error")
        return json.dumps(CANNED_VERDICTS[role])


# ── Runner ────────────────────────────────────────────────


def test_runner_invokes_all_five_agents_in_order() -> None:
    client = FakeClaudeClient()
    runner = CommitteeRunner(client=client)
    setup = SetupPayload(
        engine="e1", ticker="NVDA", structure="iron_condor",
        short_strikes=[140.0, 160.0], long_strikes=[135.0, 165.0],
        dte=14, expected_move=8.5, iv_rank=72.0,
    )
    decision = runner.deliberate(setup=setup, context={"regime": {"label": "Risk-On"}})

    assert [c[0] for c in client.calls] == [
        "researcher", "quant", "devils_advocate", "risk_officer", "synthesizer",
    ]
    assert len(decision.agent_verdicts) == 4
    assert decision.synthesis is not None
    assert decision.synthesis.parsed["decision"] == "approve"
    assert decision.synthesis.role == AgentRole.SYNTHESIZER
    # All four committee verdicts should parse successfully.
    for v in decision.agent_verdicts:
        assert v.error is None
        assert v.parsed != {}


def test_runner_handles_individual_agent_failure() -> None:
    client = FakeClaudeClient(fail_role=AgentRole.DEVILS_ADVOCATE)
    runner = CommitteeRunner(client=client)
    setup = SetupPayload(engine="e1", ticker="MSFT", structure="iron_condor")
    decision = runner.deliberate(setup=setup, context={})

    devils = next(v for v in decision.agent_verdicts if v.role == AgentRole.DEVILS_ADVOCATE)
    assert devils.error and "simulated network error" in devils.error
    assert devils.parsed == {}
    # Synthesizer still runs even when one upstream fails.
    assert decision.synthesis is not None


def test_runner_records_elapsed_ms() -> None:
    client = FakeClaudeClient()
    runner = CommitteeRunner(client=client)
    setup = SetupPayload(engine="e2", ticker="SPX", structure="iron_condor")
    decision = runner.deliberate(setup=setup, context={})
    assert decision.elapsed_ms >= 0


def test_setup_payload_round_trip() -> None:
    setup = SetupPayload(engine="e1", ticker="NVDA", structure="iron_condor")
    d = setup.to_dict()
    assert d["engine"] == "e1"
    assert d["ticker"] == "NVDA"
    assert d["short_strikes"] == []


# ── Endpoint contracts ─────────────────────────────────────


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}

    def get(self, key: str) -> str | None:
        return self.kv.get(key)

    def set(self, key: str, value: str) -> None:
        self.kv[key] = value

    def xadd(self, stream: str, fields, maxlen=None, approximate=True):
        entries = self.streams.setdefault(stream, [])
        entry_id = f"{len(entries)+1}-0"
        entries.append((entry_id, dict(fields)))
        if maxlen is not None and len(entries) > maxlen:
            self.streams[stream] = entries[-maxlen:]
        return entry_id

    def xrevrange(self, stream: str, count: int = 10):
        entries = self.streams.get(stream, [])
        return list(reversed(entries[-count:]))


@pytest.fixture
def patched_client(monkeypatch: pytest.MonkeyPatch):
    from v2_app import main as v2_main
    from v2_app.routers import committee as committee_router

    fake_redis = FakeRedis()
    monkeypatch.setattr(committee_router, "_redis_client", lambda: fake_redis)

    fake_claude = FakeClaudeClient()
    committee_router.set_claude_client_factory(lambda: fake_claude)

    yield TestClient(v2_main.app), fake_redis, fake_claude

    committee_router.set_claude_client_factory(None)


def test_roles_endpoint(patched_client) -> None:
    client, _, _ = patched_client
    r = client.get("/api/v2/committee/roles")
    assert r.status_code == 200
    body = r.json()
    role_ids = [r["id"] for r in body["roles"]]
    assert role_ids == [
        "researcher", "quant", "devils_advocate", "risk_officer", "synthesizer",
    ]


def test_dry_run_returns_context_no_llm(patched_client) -> None:
    client, _, fake_claude = patched_client
    r = client.post(
        "/api/v2/committee/dry-run",
        json={
            "setup": {
                "engine": "e1", "ticker": "NVDA", "structure": "iron_condor",
                "short_strikes": [140, 160], "long_strikes": [135, 165],
                "dte": 14,
            },
            "market_state": None,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["setup"]["ticker"] == "NVDA"
    assert "context" in body
    # Dry-run must not call the LLM at all.
    assert fake_claude.calls == []


def test_deliberate_full_round_trip(patched_client) -> None:
    client, fake_redis, fake_claude = patched_client
    r = client.post(
        "/api/v2/committee/deliberate",
        json={
            "setup": {
                "engine": "e1", "ticker": "NVDA", "structure": "iron_condor",
                "short_strikes": [140, 160], "long_strikes": [135, 165],
                "dte": 14,
            },
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert len(body["agent_verdicts"]) == 4
    assert body["synthesis"]["parsed"]["decision"] == "approve"
    # Five LLM calls total: 4 committee + 1 synthesis.
    assert len(fake_claude.calls) == 5
    # Persistence to the redis stream.
    entries = fake_redis.streams.get("v2:committee:deliberations", [])
    assert len(entries) == 1


def test_recent_endpoint_returns_persisted_decisions(patched_client) -> None:
    client, _, _ = patched_client
    # Run two deliberations.
    payload = {
        "setup": {"engine": "e1", "ticker": "NVDA", "structure": "iron_condor"},
    }
    client.post("/api/v2/committee/deliberate", json=payload)
    client.post(
        "/api/v2/committee/deliberate",
        json={"setup": {"engine": "e2", "ticker": "SPX", "structure": "iron_condor"}},
    )
    r = client.get("/api/v2/committee/recent?n=5")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["n"] == 2
    tickers = {e["ticker"] for e in body["entries"]}
    assert tickers == {"NVDA", "SPX"}


def test_deliberate_validates_setup(patched_client) -> None:
    client, _, _ = patched_client
    # Missing required ticker.
    r = client.post(
        "/api/v2/committee/deliberate",
        json={"setup": {"engine": "e1", "structure": "iron_condor"}},
    )
    assert r.status_code == 422


def test_committee_flag_in_version(patched_client) -> None:
    client, _, _ = patched_client
    r = client.get("/api/v2/version")
    assert r.status_code == 200
    body = r.json()
    assert body["foundation"]["agent_committee"] is True
